"""Runtime configuration for the authgate service.

authgate is a small, separate service (NOT part of the MCP image) whose only
job is to mint YouTube session cookies through a human login in a server-side
browser, then hand them to the transcription MCP over the shared workspace
volume. Kept intentionally minimal; every setting here is consumed somewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class AuthgateConfig:
    host: str
    port: int
    # Where session state (JSON) is persisted so a container restart does not
    # lose active sessions/locks (diagnostic critique #4 of the prototype).
    state_dir: Path
    # The cookies file the MCP reads. MUST live on the volume both services
    # mount (the MCP's per-run subdir), so the handoff needs no network.
    managed_cookies_file: Path
    # Sliding TTL: the cookies file is deleted after this many seconds WITHOUT
    # use. The MCP touches its mtime on every successful cookie-backed
    # download, so constant activity keeps it alive; 24h idle drops it.
    cookie_idle_ttl_s: int
    # How long a login window stays open before it expires and the browser is
    # torn down (the human must finish logging in within this window).
    session_ttl_s: int
    # Page the remote browser opens for the human to log in.
    login_start_url: str
    # Presence of ANY of these cookies (on youtube/google) means "logged in".
    auth_cookie_names: tuple[str, ...]
    # Only cookies on these domains are exported (keeps the file minimal).
    export_domains: tuple[str, ...]
    # X display the headed Chromium renders into; x11vnc mirrors it.
    display: str

    @classmethod
    def from_env(cls) -> "AuthgateConfig":
        return cls(
            host=_str("AUTHGATE_HOST", "0.0.0.0"),
            port=_int("AUTHGATE_PORT", 8080),
            state_dir=Path(_str("AUTHGATE_STATE_DIR", "/state")),
            managed_cookies_file=Path(
                _str(
                    "AUTHGATE_MANAGED_COOKIES_FILE",
                    "/mcp-workspace/transcription-mcp/secrets/youtube-cookies.txt",
                )
            ),
            cookie_idle_ttl_s=_int("AUTHGATE_COOKIE_IDLE_TTL_S", 86_400),
            session_ttl_s=_int("AUTHGATE_SESSION_TTL_S", 900),
            login_start_url=_str(
                "AUTHGATE_LOGIN_START_URL",
                "https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/",
            ),
            auth_cookie_names=_csv(
                "AUTHGATE_AUTH_COOKIE_NAMES", ("__Secure-1PSID", "__Secure-3PSID", "SID")
            ),
            export_domains=_csv(
                "AUTHGATE_EXPORT_DOMAINS", (".youtube.com", ".google.com")
            ),
            display=_str("AUTHGATE_DISPLAY", ":99"),
        )
