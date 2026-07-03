#!/bin/sh
# Container entrypoint.
#
# YouTube breaks yt-dlp extractors every few weeks; a stale extractor surfaces
# as HTTP 403 / "unable to extract" errors that look like IP blocking but are
# not. To keep the Groq tier alive without rebuilding the image, we refresh
# yt-dlp to the latest release (including nightly pre-releases) on every
# container start.
#
# Design constraints:
#   - Best effort: an offline or PyPI-down start must NOT prevent the server
#     from booting. Failures are logged and ignored.
#   - Bounded: the update attempt is capped so a slow network cannot delay
#     startup indefinitely (HEALTHCHECK start-period is 40s).
#   - Opt-out: set YTDLP_AUTO_UPDATE=0 for strictly reproducible deployments
#     (e.g. air-gapped or compliance-pinned environments).
#
# Only yt-dlp is updated. Everything else stays exactly as resolved by
# `uv sync --frozen` at build time, so the reproducibility loss is confined
# to the one package whose freshness is an operational requirement.

set -u

YTDLP_AUTO_UPDATE="${YTDLP_AUTO_UPDATE:-1}"
YTDLP_UPDATE_TIMEOUT="${YTDLP_UPDATE_TIMEOUT:-25}"
VENV_PYTHON="/app/.venv/bin/python"

if [ "$YTDLP_AUTO_UPDATE" = "1" ]; then
    echo "[entrypoint] refreshing yt-dlp (nightly channel, timeout ${YTDLP_UPDATE_TIMEOUT}s)..."
    if timeout "$YTDLP_UPDATE_TIMEOUT" \
        uv pip install --python "$VENV_PYTHON" --prerelease=allow --upgrade --quiet yt-dlp; then
        echo "[entrypoint] yt-dlp now at: $("$VENV_PYTHON" -c 'import yt_dlp; print(yt_dlp.version.__version__)')"
    else
        echo "[entrypoint] WARNING: yt-dlp update failed or timed out; continuing with built-in version" >&2
    fi
else
    echo "[entrypoint] YTDLP_AUTO_UPDATE=0, skipping yt-dlp refresh"
fi

exec "$@"
