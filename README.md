# MarkItDown REST API

A FastAPI-based REST API service that converts various file formats to Markdown using Microsoft's MarkItDown library.

## Features

- Convert single files to Markdown format
- Batch conversion support
- Download converted files or get JSON response
- Fetch and convert files directly from remote URLs
- Smart pre-processing with Google Magika to skip unnecessary conversions for plain text
- Graceful error handling for LLM rate limits (429) and provider outages (503)
- Image OCR support (requires OpenAI API key)
- Support for multiple file formats:
  - Documents: PDF, DOCX, PPTX, XLSX
  - Images: PNG, JPG, JPEG, GIF, BMP, TIFF
  - Audio: MP3, WAV, M4A, OGG
  - Web: HTML, XML
  - Data: CSV, JSON
  - Text: TXT, MD, RTF
  - Other: EPUB, ZIP

## Development Tools

This project uses modern development tools for improved developer experience:

### Task Runner (Taskfile)

We use [Task](https://taskfile.dev/) instead of traditional Makefiles for task automation. Task provides:

- Cross-platform compatibility (works on Windows, macOS, Linux)
- YAML syntax that's more readable than Makefiles
- Built-in variable support and dependency management
- Better error handling and output formatting

### API Testing (Hurl)

API testing is done with [Hurl](https://hurl.dev/) instead of traditional curl scripts or Postman collections:

- Tests are written in plain text files that are easy to version control
- Human-readable format that serves as living documentation
- Built-in assertions and JSON path support
- Can be integrated into CI/CD pipelines
- Faster than UI-based testing tools

## Installation

1. Create virtual environment and install dependencies:
```bash
task install
```

Or manually:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Start the server:
```bash
task start
```

Or manually:
```bash
python main.py
```

The API will be available at `http://localhost:8000`

### Environment Variables

Create a `.env` file (see `.env.example`):

- `MODEL_PROVIDER` - Optional: `openai` (default), `azure`, or `openai-compatible`
- `MODEL` - Optional: Default model/deployment name for OpenAI-compatible providers (`gpt-4o` by default)
- `OPENAI_API_KEY` - Optional: API key for the default OpenAI provider (also used as fallback for other providers and to enable OCR)
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` - Required when `MODEL_PROVIDER=azure`
- `OPENAI_BASE_URL`, `OPENAI_API_KEY` - Required when `MODEL_PROVIDER=openai-compatible` (Groq, LiteLLM, etc.). Legacy variables `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY` are also supported.
- `OPENAI_API_KEY_PROVIDER` - Optional: set to `oauth2` to fetch API tokens via client credentials (currently supported with `MODEL_PROVIDER=openai-compatible`). Requires `OAUTH_TOKEN_URL`, `OAUTH_CLIENT_ID`, and `OAUTH_CLIENT_SECRET`.

#### Provider Configuration Examples

OpenAI (default):
```bash
MODEL_PROVIDER=openai
OPENAI_API_KEY=sk-...
MODEL=gpt-4o
```

Azure OpenAI:
```bash
MODEL_PROVIDER=azure
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_API_VERSION=2024-02-15-preview
```

Groq or other OpenAI-compatible providers (via LiteLLM, etc.):
```bash
MODEL_PROVIDER=openai-compatible
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_API_KEY=gsk-...
MODEL=meta-llama/llama-4-maverick-17b-128e-instruct
```

OpenAI-compatible provider with OAuth2 client credentials (see `Taskfile.apollo.yaml`):
```bash
MODEL_PROVIDER=openai-compatible
OPENAI_BASE_URL=https://api.partner.com/openai/v1
OPENAI_API_KEY_PROVIDER=oauth2
OAUTH_TOKEN_URL=https://auth.partner.com/oauth/token
OAUTH_CLIENT_ID=...
OAUTH_CLIENT_SECRET=...
MODEL=my-model
```

All providers share the same conversion endpoints and benefit from the built-in rate limit (429) and outage (503) handling.

## API Endpoints

### Convert Single File
```bash
POST /api/v1/convert
```

Upload a file to convert to Markdown. Optional query parameters:
- `download=true` to download the result as a `.md` file
- `file_url` to fetch and convert a remote file (HTTP/HTTPS, ≤50 MB)

Example:
```bash
curl -X POST "http://localhost:8000/api/v1/convert" \
  -F "file=@document.pdf"
```

Convert using a remote URL:
```bash
curl -X POST "http://localhost:8000/api/v1/convert" \
  -d "file_url=https://example.com/document.pdf"
```

### Batch Convert Files
```bash
POST /api/v1/convert/batch
```

Convert multiple files in a single request.

Example:
```bash
curl -X POST "http://localhost:8000/api/v1/convert/batch" \
  -F "files=@doc1.pdf" \
  -F "files=@doc2.docx"
```

### Health Check
```bash
GET /health
```

## API Documentation

Interactive API documentation available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Testing

All API tests are written in Hurl format and located in `tests/hurl/`. Tests serve as both validation and documentation.

Run all tests:
```bash
task test
```

Test specific endpoints:
```bash
task test:health    # Health endpoints
task test:convert   # Conversion endpoints  
task test:image     # Image OCR (requires OPENAI_API_KEY)
```

### Available Task Commands

View all available tasks:

```bash
task --list
```

Common development tasks:

```bash
task install       # Install dependencies
task start         # Start the API server
task dev           # Start with auto-reload
task test          # Run all tests
```

## Docker

Build and run with Docker:

```bash
# Build the image
docker build -t markitdown-api .

# Run the container
docker run -p 8000:8000 markitdown-api

# With environment variables
docker run -p 8000:8000 -e OPENAI_API_KEY=your_key markitdown-api
```

## Response Format

### Single File Conversion
```json
{
  "filename": "document.pdf",
  "original_format": ".pdf",
  "markdown_content": "# Converted content...",
  "metadata": {
    "file_size": 12345,
    "converted_at": 1234567890.123,
    "detection": {
      "label": "pdf",
      "mime_type": "application/pdf",
      "group": "document",
      "is_text": false,
      "score": 0.997
    }
  },
  "conversion_time": 0.234
}
```

### Batch Conversion
```json
{
  "successful_conversions": [...],
  "errors": [...],
  "total_files": 3,
  "successful": 2,
  "failed": 1
}
```
