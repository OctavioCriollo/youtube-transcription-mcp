"""Runtime configuration loaded from environment variables.

Kept intentionally small. Every variable here is consumed somewhere; nothing
is here "for the future".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


VALID_TRANSPORTS = {"stdio", "streamable-http"}
APP_DIR_NAME = "transcription-mcp"


class ConfigError(RuntimeError):
    """Raised at startup when configuration is invalid."""


def _default_workspace_dir() -> Path:
    """Return an OS-standard per-user workspace directory.

    `WORKSPACE_DIR` is the explicit override for Docker, servers, and operators
    that want a mounted volume. The implicit default intentionally avoids probing
    absolute paths such as `/workspace`, which resolves to a drive-root path on
    Windows.
    """
    return _default_app_data_dir() / "workspace"


def _default_app_data_dir() -> Path:
    if sys.platform == "win32":
        base = _path_from_env("LOCALAPPDATA") or _path_from_env("APPDATA") or Path.home()
        return base / APP_DIR_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME

    base = _path_from_env("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return base / APP_DIR_NAME


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return Path(stripped).expanduser()


@dataclass(frozen=True)
class Config:
    workspace_dir: Path
    transport: str
    host: str
    port: int
    http_path: str
    ytdlp_cookies_file: Path | None
    ytdlp_proxy: str | None
    cache_ttl_hours: float | None
    max_concurrent_jobs: int
    job_ttl_hours: float | None

    @classmethod
    def from_env(cls) -> "Config":
        # GROQ_API_KEY is resolved lazily by the vendored
        # transcription_v4.providers.GroqProvider, which checks (in order):
        #   1. GROQ_API_KEY env var
        #   2. storage/secrets/groq.key in project root or any parent dir
        #   3. TRANSCRIPTION_V4_SECRETS_DIR/groq.key
        # We do not validate here so the server can boot even when only
        # the subtitles path is used (which does not need Groq at all).
        workspace_raw = os.environ.get("WORKSPACE_DIR")
        workspace = (
            Path(workspace_raw).expanduser().resolve()
            if workspace_raw
            else _default_workspace_dir()
        )

        transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
        if transport not in VALID_TRANSPORTS:
            allowed = ", ".join(sorted(VALID_TRANSPORTS))
            raise ConfigError(
                f"MCP_TRANSPORT must be one of: {allowed}; got {transport!r}"
            )

        port_raw = os.environ.get("MCP_PORT", "8000")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"MCP_PORT must be an integer, got {port_raw!r}") from exc
        if not 1 <= port <= 65535:
            raise ConfigError(f"MCP_PORT must be between 1 and 65535, got {port}")

        http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")
        if not http_path.startswith("/"):
            raise ConfigError(f"MCP_HTTP_PATH must start with '/', got {http_path!r}")

        cookies_file = _optional_path("YT_COOKIES_FILE")
        if cookies_file is not None and not cookies_file.is_file():
            raise ConfigError(f"YT_COOKIES_FILE does not exist or is not a file: {cookies_file}")

        return cls(
            workspace_dir=workspace,
            transport=transport,
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=port,
            http_path=http_path,
            ytdlp_cookies_file=cookies_file,
            ytdlp_proxy=_optional_string("YT_PROXY"),
            cache_ttl_hours=_optional_float_env("MCP_CACHE_TTL_HOURS", default=24.0),
            max_concurrent_jobs=_int_env("MCP_MAX_CONCURRENT_JOBS", default=2, minimum=1),
            job_ttl_hours=_optional_float_env("MCP_JOB_TTL_HOURS", default=168.0),
        )

    @property
    def v4_storage_dir(self) -> Path:
        return self.workspace_dir / "v4-storage"

    def ensure_directories(self) -> None:
        self.v4_storage_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "mcp-jobs").mkdir(parents=True, exist_ok=True)


def _optional_string(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _optional_path(name: str) -> Path | None:
    value = _optional_string(name)
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def _optional_float_env(name: str, *, default: float | None) -> float | None:
    value = _optional_string(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if parsed <= 0:
        return None
    return parsed


def _int_env(name: str, *, default: int, minimum: int) -> int:
    value = _optional_string(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed
