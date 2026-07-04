# authgate: human-in-the-loop login gateway that mints YouTube cookies.
#
# Built from a plain Python base (not the Playwright registry image) so the tag
# is always valid and the browser version comes from pip + `playwright install`
# — reproducible without depending on a specific registry tag existing.
#
# Build context is the REPO ROOT (so CI can build MCP + authgate from one tree):
#   docker build -f authgate/Dockerfile -t authgate .
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Display stack for the remote browser: virtual X server, VNC mirror, and the
# noVNC web client + websockify bridge. net-tools/procps aid debugging on the VPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the app, then the matching Chromium + its system libraries. Pinning
# playwright here keeps the browser build reproducible; --with-deps pulls the
# apt libraries Chromium needs.
COPY authgate/pyproject.toml ./
COPY authgate/src ./src
RUN pip install . "playwright==1.55.0" \
    && playwright install --with-deps chromium

COPY authgate/supervisor.sh /supervisor.sh
RUN chmod +x /supervisor.sh

ENV AUTHGATE_HOST=0.0.0.0 \
    AUTHGATE_PORT=8080 \
    AUTHGATE_DISPLAY=:99 \
    AUTHGATE_STATE_DIR=/state

# 6080 = noVNC/websockify (via Traefik). 8080 = internal API + ForwardAuth.
EXPOSE 6080 8080

# Liveness: the internal API answering means the app is up. The display stack
# is best-effort and logged by the supervisor.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)" || exit 1

ENTRYPOINT ["/supervisor.sh"]
