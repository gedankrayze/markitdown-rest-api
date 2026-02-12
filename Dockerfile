FROM python:3.14-slim

# Install system dependencies for markitdown
RUN apt-get update && apt-get install -y \
    # For PDF processing
    poppler-utils \
    # For audio processing
    ffmpeg \
    # Clean up
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY app/ ./app/
COPY public/ ./public/

# Create non-root user
RUN useradd -m -u 1000 apiuser && chown -R apiuser:apiuser /app

USER apiuser

# Expose port
EXPOSE 8000

# Run the application
CMD ["python", "main.py"]