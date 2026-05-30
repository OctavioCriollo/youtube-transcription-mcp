"""Runtime configuration loaded from environment variables.

Kept intentionally small. Every variable here is consumed somewhere; nothing
is here "for the future".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


VALID_TRANSPORTS = {"stdio", "streamable-http"}


class ConfigError(RuntimeError):
    """Raised at startup when configuration is invalid."""


def _default_workspace_dir() -> Path:
    """Choose a writable workspace dir that works for `uvx`-launched runs.

    Inside a Docker container we want /workspace (mounted as a volume).
    For uvx-launched (running under the OpenClaw user) we use ~/.transcription-mcp.
    """
    if Path("/workspace").exists() and os.access("/workspace", os.W_OK):
        return Path("/workspace")
    return Path.home() / ".transcription-mcp" / "workspace"


@dataclass(frozen=True)
class Config:
    workspace_dir: Path
    transport: str
    host: str
    port: int
    http_path: str

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

        return cls(
            workspace_dir=workspace,
            transport=transport,
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=port,
            http_path=http_path,
        )

    @property
    def v4_storage_dir(self) -> Path:
        return self.workspace_dir / "v4-storage"

    def ensure_directories(self) -> None:
        self.v4_storage_dir.mkdir(parents=True, exist_ok=True)
