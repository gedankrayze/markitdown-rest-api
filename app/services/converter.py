import logging
import os
import time
from typing import Any, Dict, Optional, Sequence, Tuple

import httpx
from markitdown import MarkItDown

try:
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AzureOpenAI,
        OpenAI,
        OpenAIError,
        RateLimitError,
    )

    openai_available = True
except ImportError:
    OpenAI = None
    AzureOpenAI = None  # type: ignore[assignment]
    APIStatusError = None  # type: ignore[assignment]
    RateLimitError = None  # type: ignore[assignment]
    APITimeoutError = None  # type: ignore[assignment]
    APIConnectionError = None  # type: ignore[assignment]
    OpenAIError = None  # type: ignore[assignment]
    openai_available = False

try:
    from magika import Magika

    magika_available = True
except ImportError:
    Magika = None  # type: ignore[assignment]
    magika_available = False

# Configure logger
logger = logging.getLogger(__name__)


class ConversionRateLimitError(Exception):
    """Raised when upstream LLM provider returns a rate limit error."""


class ConversionProviderUnavailableError(Exception):
    """Raised when upstream LLM provider is unavailable or returns 5xx."""


class ConversionService:
    """High-level orchestration around MarkItDown conversions."""

    _TEXT_MIME_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}

    def __init__(self):
        provider = (os.getenv("MODEL_PROVIDER") or "openai").strip().lower()
        self.llm_provider = provider
        self._oauth_token_expires_in: Optional[int] = None
        logger.info("MODEL_PROVIDER set to '%s'", provider or "openai")
        client, model_name = self._create_llm_client(provider)
        self.llm_model_name = model_name
        self.llm_enabled = client is not None

        if client and model_name:
            self.converter = MarkItDown(llm_client=client, llm_model=model_name)
            logger.info(
                "MarkItDown ready with %s provider (%s)", provider, model_name
            )
        else:
            self.converter = MarkItDown()
            if provider != "openai":
                logger.info(
                    "MarkItDown ready without external LLM (provider '%s' unavailable)",
                    provider,
                )
            elif not openai_available:
                logger.warning(
                    "MarkItDown ready (openai package missing; external LLM disabled)"
                )
            else:
                logger.info("MarkItDown ready (no LLM provider configured)")

        if magika_available:
            try:
                self.detector: Optional[Magika] = Magika()
                logger.info("Magika content triage enabled")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Magika initialization failed: %s", exc)
                self.detector = None
        else:
            self.detector = None
            logger.debug("Magika not available; skipping pre-conversion triage")

    def convert_file(self, file_path: str) -> tuple[str, Dict[str, Any], float]:
        """
        Convert a file to markdown using MarkItDown.

        Args:
            file_path: Path to the file to convert

        Returns:
            tuple: (markdown_content, metadata, conversion_time)
        """
        file_name = os.path.basename(file_path)
        file_extension = self.get_file_extension(file_name)
        file_size = os.path.getsize(file_path)

        logger.info("Converting %s (%s, %s bytes)", file_name, file_extension, file_size)
        start_time = time.time()

        detection = self._identify_content_type(file_path)

        try:
            if self._can_short_circuit_to_text(detection, file_extension):
                text_content = self._read_text_file(file_path)
                conversion_time = time.time() - start_time
                metadata = self._build_metadata(file_size, detection)
                logger.info("Bypassed MarkItDown for plain text %s", file_name)
                return text_content, metadata, conversion_time

            result = self.converter.convert(file_path)
            conversion_time = time.time() - start_time

            metadata = self._build_metadata(file_size, detection)

            logger.info("Converted %s in %.2fs", file_name, conversion_time)
            return result.text_content, metadata, conversion_time

        except (ConversionRateLimitError, ConversionProviderUnavailableError):
            raise
        except Exception as exc:  # pragma: no cover - surfaces to API caller
            if RateLimitError and isinstance(exc, RateLimitError):
                logger.warning("Rate limited while converting %s: %s", file_name, exc)
                raise ConversionRateLimitError(str(exc)) from exc

            status_related = (
                (APIStatusError and isinstance(exc, APIStatusError))
                or (APIConnectionError and isinstance(exc, APIConnectionError))
                or (APITimeoutError and isinstance(exc, APITimeoutError))
            )

            if status_related:
                status_code = getattr(exc, "status_code", None)
                message = str(exc)
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    logger.error("Conversion failed for %s: %s", file_name, message)
                    raise Exception(f"Conversion failed: {message}") from exc
                logger.error(
                    "Upstream provider unavailable for %s (status: %s): %s",
                    file_name,
                    status_code,
                    message,
                )
                raise ConversionProviderUnavailableError(str(exc)) from exc

            if OpenAIError and isinstance(exc, OpenAIError):
                logger.error("LLM provider error converting %s: %s", file_name, exc)
                raise Exception(f"Conversion failed: {str(exc)}") from exc

            logger.error("Failed to convert %s: %s", file_name, str(exc))
            raise Exception(f"Conversion failed: {str(exc)}") from exc

    def get_file_extension(self, filename: str) -> str:
        """Extract file extension from filename."""
        return os.path.splitext(filename)[1].lower()

    def is_supported_format(self, filename: str) -> bool:
        """Check if file format is supported by MarkItDown."""
        supported_extensions = {
            ".pdf",
            ".pptx",
            ".docx",
            ".xlsx",
            ".xls",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".tiff",
            ".mp3",
            ".wav",
            ".m4a",
            ".ogg",
            ".html",
            ".htm",
            ".xml",
            ".csv",
            ".json",
            ".txt",
            ".md",
            ".rtf",
            ".epub",
            ".zip",
        }

        ext = self.get_file_extension(filename)
        return ext in supported_extensions

    # --- Internal helpers -------------------------------------------------

    def _create_llm_client(
        self, provider: str
    ) -> Tuple[Optional[Any], Optional[str]]:
        if not openai_available:
            return None, None

        provider = provider or "openai"

        if provider == "openai":
            api_key = self._resolve_api_key(["OPENAI_API_KEY"])
            model = os.getenv("MODEL", "gpt-4o")
            if not api_key:
                logger.debug("OPENAI_API_KEY not set; skipping OpenAI client")
                return None, None
            return OpenAI(api_key=api_key), model

        if provider == "azure":
            api_key = self._resolve_api_key(
                ["AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"]
            )
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("MODEL")
            api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

            if not AzureOpenAI:
                logger.warning("AzureOpenAI client not available in openai package")
                return None, None

            if not api_key:
                logger.warning(
                    "Azure OpenAI configuration missing API key; check environment or OAuth provider"
                )
                return None, None

            if not all([endpoint, deployment]):
                logger.warning(
                    "Azure OpenAI configuration incomplete (requires AZURE_OPENAI_API_KEY, "
                    "AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT)"
                )
                return None, None

            client = AzureOpenAI(
                api_key=api_key,
                azure_endpoint=endpoint,
                api_version=api_version,
            )
            return client, deployment

        if provider == "openai-compatible":
            base_url = (
                os.getenv("OPENAI_BASE_URL")
                or os.getenv("OPENAI_COMPATIBLE_BASE_URL")
            )
            api_key = self._resolve_api_key(
                ["OPENAI_API_KEY", "OPENAI_COMPATIBLE_API_KEY"]
            )
            model = os.getenv("MODEL", "gpt-4o")

            if not base_url:
                logger.warning(
                    "OPENAI_BASE_URL not set; cannot initialize compatible client"
                )
                return None, None

            if not api_key:
                logger.warning(
                    "OPENAI_BASE_URL is set but no API key available; check OPENAI_API_KEY or OAuth provider configuration"
                )
                return None, None

            client = OpenAI(api_key=api_key, base_url=base_url)
            return client, model

        if provider not in {"", "none"}:
            logger.warning("Unknown MODEL_PROVIDER '%s'; falling back to default", provider)
        return None, None

    def _resolve_api_key(self, candidates: Sequence[str]) -> Optional[str]:
        key_provider = (os.getenv("OPENAI_API_KEY_PROVIDER") or "").strip().lower()

        if key_provider == "oauth2":
            if getattr(self, "llm_provider", None) != "openai-compatible":
                logger.warning(
                    "OPENAI_API_KEY_PROVIDER=oauth2 is currently supported only when MODEL_PROVIDER=openai-compatible"
                )
            else:
                token = self._fetch_oauth2_token()
                if token:
                    return token
                logger.error(
                    "OPENAI_API_KEY_PROVIDER=oauth2 but failed to obtain access token. "
                    "Falling back to environment variables."
                )
        elif key_provider and key_provider not in {"env", "environment"}:
            logger.warning(
                "Unsupported OPENAI_API_KEY_PROVIDER '%s'; defaulting to environment variables",
                key_provider,
            )

        for name in candidates:
            value = os.getenv(name)
            if value:
                return value
        return None

    def _fetch_oauth2_token(self) -> Optional[str]:
        token_url = os.getenv("OAUTH_TOKEN_URL")
        client_id = os.getenv("OAUTH_CLIENT_ID")
        client_secret = os.getenv("OAUTH_CLIENT_SECRET")

        if not all([token_url, client_id, client_secret]):
            logger.error(
                "OAuth2 API key provider requires OAUTH_TOKEN_URL, OAUTH_CLIENT_ID, and OAUTH_CLIENT_SECRET"
            )
            return None

        try:
            response = httpx.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=httpx.Timeout(15.0, connect=5.0),
            )
            response.raise_for_status()
        except Exception as exc:
            logger.error("Failed to obtain OAuth2 access token: %s", exc)
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.error("OAuth2 token response is not valid JSON")
            return None

        token = payload.get("access_token")
        if not token:
            logger.error("OAuth2 token response did not include access_token")
            return None

        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)):
            self._oauth_token_expires_in = int(expires_in)

        return token

    def _identify_content_type(self, file_path: str):
        if not self.detector:
            return None
        try:
            result = self.detector.identify_path(file_path)
            if not result or not result.ok:
                return None
            return result
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Magika identification failed for %s: %s", file_path, exc)
            return None

    def _can_short_circuit_to_text(self, detection, file_extension: str) -> bool:
        if detection and detection.output.mime_type in self._TEXT_MIME_TYPES:
            return True
        # Fall back to extension heuristics when Magika is unavailable
        return file_extension in {".txt", ".md", ".markdown"}

    def _read_text_file(self, file_path: str) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                return handle.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()

    def _build_metadata(self, file_size: int, detection) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "file_size": file_size,
            "converted_at": time.time(),
        }
        if detection:
            metadata["detection"] = {
                "label": str(detection.output.label),
                "mime_type": detection.output.mime_type,
                "group": detection.output.group,
                "is_text": detection.output.is_text,
                "score": detection.score,
            }
        metadata["llm_provider"] = (
            self.llm_provider if self.llm_enabled else "none"
        )
        if self.llm_enabled and self.llm_model_name:
            metadata["llm_model"] = self.llm_model_name
        return metadata
