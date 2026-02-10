import logging
import mimetypes
import os
import ipaddress
import socket
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.models.conversion import ConversionResponse
from app.services.converter import (
    ConversionProviderUnavailableError,
    ConversionRateLimitError,
    ConversionService,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversion"])
converter_service = ConversionService()

CHUNK_SIZE = 1024 * 1024  # 1MB chunks to avoid loading whole files into memory
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB safety cap for uploads
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50MB safety cap for remote downloads
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


@router.post("/convert", response_model=ConversionResponse)
async def convert_file(
    file: Optional[UploadFile] = File(None),
    download: Optional[bool] = False,
    file_url: Optional[str] = None,
):
    """
    Convert uploaded file or remote file (by URL) to Markdown format.

    Args:
        file: File to convert (multipart/form-data upload)
        download: If True, return markdown as downloadable file
        file_url: Optional HTTP/HTTPS URL to fetch and convert

    Returns:
        ConversionResponse with markdown content
    """

    if not file and not file_url:
        raise HTTPException(
            status_code=400,
            detail="Provide either an uploaded file or file_url parameter.",
        )

    if file and file_url:
        raise HTTPException(
            status_code=400,
            detail="Provide either an uploaded file or file_url parameter, not both.",
        )

    temp_file_path = None
    filename = "upload"

    if file:
        filename = file.filename or filename
    else:
        filename, temp_file_path = await _download_file_to_temp(file_url)
        logger.info("Downloaded file from URL: %s", file_url)

    logger.info("Request: %s", filename)

    if not converter_service.is_supported_format(filename):
        logger.warning("Unsupported format: %s", filename)
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format. File: {filename}",
        )

    try:
        if not temp_file_path:
            suffix = os.path.splitext(filename)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                temp_file_path = tmp_file.name
                total_bytes = 0
                while True:
                    chunk = await file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail="Uploaded file exceeds maximum allowed size of 50MB.",
                        )
                    tmp_file.write(chunk)

        markdown_content, metadata, conversion_time = await run_in_threadpool(
            converter_service.convert_file,
            temp_file_path,
        )

        if download:
            markdown_filename = os.path.splitext(filename)[0] + ".md"
            return Response(
                content=markdown_content,
                media_type="text/markdown",
                headers={
                    "Content-Disposition": f"attachment; filename={markdown_filename}"
                },
            )

        return ConversionResponse(
            filename=filename,
            original_format=converter_service.get_file_extension(filename),
            markdown_content=markdown_content,
            metadata=metadata,
            conversion_time=conversion_time,
        )

    except ConversionRateLimitError as exc:
        logger.warning("Rate limit reached while processing %s: %s", filename, exc)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded while processing the file. Please retry later.",
        )
    except ConversionProviderUnavailableError as exc:
        logger.error("LLM provider unavailable for %s: %s", filename, exc)
        raise HTTPException(
            status_code=503,
            detail="Upstream conversion provider is temporarily unavailable. Please try again later.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Conversion failed for %s", filename)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.unlink(temp_file_path)


@router.post("/convert/batch")
async def convert_files_batch(
    files: list[UploadFile] = File(...)
):
    """
    Convert multiple files to Markdown format

    Args:
        files: List of files to convert

    Returns:
        List of ConversionResponse objects
    """
    results = []
    errors = []

    for file in files:
        try:
            result = await convert_file(file=file)
            results.append(result)
        except HTTPException as e:
            errors.append({
                "filename": file.filename,
                "error": e.detail
            })

    return {
        "successful_conversions": results,
        "errors": errors,
        "total_files": len(files),
        "successful": len(results),
        "failed": len(errors)
    }


def _extract_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    candidate = Path(parsed.path).name
    return candidate or "downloaded_file"


def _resolve_suffix(filename: str, content_type: Optional[str]) -> str:
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return ""


def _is_public_ip_address(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False

    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _validate_public_http_url(file_url: str) -> None:
    parsed = urlparse(file_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=400,
            detail="Only HTTP and HTTPS URLs are supported.",
        )

    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="Invalid file_url host.")

    lowered_host = host.lower()
    if lowered_host in {"localhost"} or lowered_host.endswith(".localhost"):
        raise HTTPException(
            status_code=400,
            detail="URL host is not allowed.",
        )

    if _is_public_ip_address(host):
        return

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve URL host: {exc}",
        )

    resolved_ips = {info[4][0] for info in infos if info and info[4]}
    if not resolved_ips:
        raise HTTPException(
            status_code=400,
            detail="Could not resolve URL host.",
        )

    for ip_value in resolved_ips:
        if not _is_public_ip_address(ip_value):
            raise HTTPException(
                status_code=400,
                detail="URL host resolves to a non-public address and is not allowed.",
            )


async def _download_file_to_temp(file_url: Optional[str]) -> Tuple[str, str]:
    if not file_url:
        raise HTTPException(status_code=400, detail="file_url parameter is required.")

    filename = _extract_filename_from_url(file_url)
    temp_file_path = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=False) as client:
            current_url = file_url
            response = None

            for _ in range(MAX_REDIRECTS + 1):
                _validate_public_http_url(current_url)

                async with client.stream("GET", current_url) as current_response:
                    if current_response.status_code in REDIRECT_STATUS_CODES:
                        location = current_response.headers.get("location")
                        if not location:
                            raise HTTPException(
                                status_code=502,
                                detail="Remote server returned redirect without a location header.",
                            )
                        current_url = str(urljoin(str(current_response.request.url), location))
                        continue

                    response = current_response
                    response.raise_for_status()
                    suffix = _resolve_suffix(
                        filename, response.headers.get("content-type")
                    )

                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                        temp_file_path = tmp_file.name

                    total_bytes = 0
                    with open(temp_file_path, "wb") as destination:
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            if not chunk:
                                continue
                            total_bytes += len(chunk)
                            if total_bytes > MAX_DOWNLOAD_SIZE:
                                raise HTTPException(
                                    status_code=413,
                                    detail="Remote file exceeds maximum allowed size of 50MB.",
                                )
                            destination.write(chunk)

                    if not Path(filename).suffix and suffix:
                        filename = Path(filename).stem + suffix

                    break
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Too many redirects while downloading remote file.",
                )

        success = True
        return filename, temp_file_path

    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        try:
            detail = exc.response.text or exc.response.reason_phrase
        except httpx.ResponseNotRead:
            detail = exc.response.reason_phrase or str(status_code)
        raise HTTPException(
            status_code=status_code,
            detail=f"Failed to download file: {detail}",
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to download file: {str(exc)}",
        )
    except Exception as exc:
        logger.exception("Unexpected error while downloading file from %s", file_url)
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error downloading file: {str(exc)}",
        )
    finally:
        if not success and temp_file_path and os.path.exists(temp_file_path):
            os.unlink(temp_file_path)
