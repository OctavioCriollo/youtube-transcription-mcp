"""Tests for the server-owned provider order policy (correctives 1, 2, 7)."""

from __future__ import annotations

import anyio

PUBLIC_TRANSCRIBE_TOOLS = (
    "transcribe_youtube",
    "transcribe_media_url",
    "transcribe_file",
    "start_youtube_transcription",
    "start_media_url_transcription",
    "start_file_transcription",
)


def test_provider_order_not_in_public_tool_schema(monkeypatch, tmp_path):
    """Corrective 1: the public tools must NOT expose provider_order."""
    from mcp.server.fastmcp import FastMCP

    from transcription_mcp.config import Config
    from transcription_mcp.tools import register_tools

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    mcp = FastMCP("test")
    register_tools(mcp, Config.from_env())

    tools = {tool.name: tool for tool in anyio.run(mcp.list_tools)}
    for name in PUBLIC_TRANSCRIBE_TOOLS:
        assert name in tools, f"missing tool {name}"
        properties = (tools[name].inputSchema or {}).get("properties", {})
        assert "provider_order" not in properties, f"{name} still exposes provider_order"


def test_config_reads_provider_order_env(monkeypatch, tmp_path):
    """Corrective 2: order comes from env; lock defaults to True (server owns it)."""
    from transcription_mcp.config import Config

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_YOUTUBE_PROVIDER_ORDER", "elevenlabs,subtitles")
    monkeypatch.delenv("MCP_MEDIA_PROVIDER_ORDER", raising=False)
    monkeypatch.delenv("MCP_LOCK_PROVIDER_ORDER", raising=False)

    cfg = Config.from_env()

    assert cfg.youtube_provider_order == "elevenlabs,subtitles"
    assert cfg.media_provider_order is None  # unset -> engine default applies later
    assert cfg.lock_provider_order is True


def test_storage_dir_is_neutral_name(monkeypatch, tmp_path):
    """Corrective 7: runs live under <workspace>/storage, not v4-storage."""
    from transcription_mcp.config import STORAGE_DIR_NAME, Config

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    cfg = Config.from_env()

    assert STORAGE_DIR_NAME == "storage"
    assert cfg.storage_dir == cfg.workspace_dir / "storage"
