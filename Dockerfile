FROM python:3.14-slim

# System deps: ffmpeg for audio extraction, ca-certificates for HTTPS to Groq/YouTube.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Use the checked-in uv.lock for reproducible dependency resolution in the image.
RUN pip install --no-cache-dir uv==0.10.5

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

# yt-dlp freshness is an operational requirement (YouTube breaks extractors
# every few weeks; stale extractors surface as bogus 403s). On top of the
# frozen, reproducible sync we bump ONLY yt-dlp to the latest release,
# including nightly pre-releases. The entrypoint repeats this refresh at
# container start, so long-lived images stay current between rebuilds.
RUN uv pip install --python .venv/bin/python --prerelease=allow --upgrade yt-dlp
# yt-dlp plugin that fetches proof-of-origin (PO) tokens from a
# bgutil-ytdlp-pot-provider sidecar. Inert unless YT_POT_PROVIDER_URL points
# at a running sidecar, so it is safe to ship in every image.
RUN uv pip install --python .venv/bin/python --upgrade bgutil-ytdlp-pot-provider

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV WORKSPACE_DIR=/workspace \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_HTTP_PATH=/mcp \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# NOTE: no `VOLUME` directive on purpose. In production the workspace is provided
# by an externally-managed mount (the shared `openclaw_mcp_workspace` volume at
# /mcp-workspace, with WORKSPACE_DIR=/mcp-workspace/transcription-mcp). Declaring
# VOLUME ["/workspace"] here would make Docker spawn a throwaway anonymous volume
# on every container (re)create — orphaned cruft that accumulates over redeploys.
# Persistence is the deployer's responsibility via -v / compose volumes.
EXPOSE 8000

# Real healthcheck: hit the /health route (HTTP transport) instead of just
# checking the TCP port. /health verifies the workspace and job store are
# reachable, so a hung-but-listening MCP is reported unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:'+os.environ.get('MCP_PORT','8000')+'/health'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status==200 else 1)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "transcription_mcp.server"]
