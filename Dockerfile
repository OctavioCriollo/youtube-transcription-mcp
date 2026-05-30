FROM python:3.11-slim

# System deps: ffmpeg for audio extraction, ca-certificates for HTTPS to Groq/YouTube.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files. pyproject.toml is small enough that we don't bother
# splitting it for layer caching at this scale.
COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

ENV WORKSPACE_DIR=/workspace \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_HTTP_PATH=/mcp \
    PYTHONUNBUFFERED=1

VOLUME ["/workspace"]
EXPOSE 8000

CMD ["python", "-m", "transcription_mcp.server"]
