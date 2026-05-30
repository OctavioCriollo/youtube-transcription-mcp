"""Smoke tests: no live Groq key, no network.

These verify the server boots, config validates env correctly, and the
FastMCP server constructs without error. Full end-to-end transcription
is validated manually through OpenClaw, not here.
"""

from __future__ import annotations

import pytest


def test_config_boots_without_groq_key(monkeypatch, tmp_path):
    """Boot must not require GROQ_API_KEY; v4 resolves it lazily on first call."""
    from transcription_mcp.config import Config

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.workspace_dir == tmp_path.resolve()


def test_config_defaults_to_stdio(monkeypatch, tmp_path):
    from transcription_mcp.config import Config

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    cfg = Config.from_env()
    assert cfg.transport == "stdio"


def test_config_accepts_streamable_http(monkeypatch, tmp_path):
    from transcription_mcp.config import Config

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    cfg = Config.from_env()
    assert cfg.transport == "streamable-http"


def test_config_rejects_unknown_transport(monkeypatch, tmp_path):
    from transcription_mcp.config import Config, ConfigError

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_TRANSPORT", "telepathy")
    with pytest.raises(ConfigError, match="MCP_TRANSPORT"):
        Config.from_env()


def test_config_rejects_invalid_port(monkeypatch, tmp_path):
    from transcription_mcp.config import Config, ConfigError

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_PORT", "not-a-number")
    with pytest.raises(ConfigError, match="MCP_PORT"):
        Config.from_env()


def test_config_rejects_http_path_without_slash(monkeypatch, tmp_path):
    from transcription_mcp.config import Config, ConfigError

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_HTTP_PATH", "mcp")
    with pytest.raises(ConfigError, match="MCP_HTTP_PATH"):
        Config.from_env()


def test_server_boots(monkeypatch, tmp_path):
    from transcription_mcp.server import create_server

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    server = create_server()
    assert server is not None
    assert (tmp_path / "v4-storage").is_dir()


def test_extract_video_id_variants():
    from transcription_mcp.youtube_subtitles import extract_video_id

    cases = {
        "https://www.youtube.com/watch?v=jNQXAC9IVRw": "jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw": "jNQXAC9IVRw",
        "https://www.youtube.com/shorts/jNQXAC9IVRw": "jNQXAC9IVRw",
        "https://www.youtube.com/embed/jNQXAC9IVRw": "jNQXAC9IVRw",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=10s": "jNQXAC9IVRw",
        "jNQXAC9IVRw": "jNQXAC9IVRw",
    }
    for url, expected in cases.items():
        assert extract_video_id(url) == expected, url


def test_extract_video_id_rejects_garbage():
    from transcription_mcp.youtube_subtitles import extract_video_id

    with pytest.raises(ValueError):
        extract_video_id("https://example.com/not-a-video")
