"""v0.4.2: bounded ElevenLabs timeout + honest per-provider narrative.

Motivated by job mcpjob_20260710T053329: one stalled ElevenLabs attempt froze
the whole job for exactly 3600s (the old HTTP timeout), and the frozen
"initialized" stage plus Groq's stale yt-dlp error led the OpenClaw agent to
invent a false story ("ElevenLabs is stuck downloading with yt-dlp"). These
tests pin the countermeasures.
"""

from __future__ import annotations

from transcription_engine.providers import (
    ELEVENLABS_DEFAULT_TIMEOUT_S,
    ElevenLabsProvider,
    _float_env,
)
from transcription_mcp.jobs import _agent_guidance, summarize_engine_status
from transcription_mcp.pipeline import _provider_started_message


# --- Fix C: bounded timeout -------------------------------------------------


def test_elevenlabs_default_timeout_is_bounded_not_an_hour():
    assert ELEVENLABS_DEFAULT_TIMEOUT_S == 900.0
    assert ElevenLabsProvider().timeout_s == ELEVENLABS_DEFAULT_TIMEOUT_S


def test_elevenlabs_timeout_still_overridable_per_instance():
    assert ElevenLabsProvider(timeout_s=120.0).timeout_s == 120.0


def test_float_env_parses_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("X_TIMEOUT_TEST", "300")
    assert _float_env("X_TIMEOUT_TEST", 900.0) == 300.0
    monkeypatch.setenv("X_TIMEOUT_TEST", "not-a-number")
    assert _float_env("X_TIMEOUT_TEST", 900.0) == 900.0
    monkeypatch.setenv("X_TIMEOUT_TEST", "-5")
    assert _float_env("X_TIMEOUT_TEST", 900.0) == 900.0


# --- Fix B: honest narrative ------------------------------------------------


def test_engine_summary_explains_opaque_elevenlabs_phase():
    report = {
        "stage": "initialized",
        "run_dir": "/tmp/run",
        "model": {"provider": "elevenlabs"},
        "chunking": {},
    }
    summary = summarize_engine_status(report)
    message = summary["message"]
    assert "nothing is downloaded on this server" in message
    assert "remotely" in message
    # Chunked paths keep their specific progress message.
    chunked = summarize_engine_status(
        {
            "stage": "transcribing",
            "run_dir": "/tmp/run",
            "model": {"provider": "elevenlabs"},
            "chunking": {"expected_chunks": 4, "partials": 2},
        }
    )
    assert "2/4" in chunked["message"]


def test_provider_started_messages_describe_mechanics():
    eleven = _provider_started_message("elevenlabs", "youtube")
    assert "its own servers" in eleven
    assert "Nothing is downloaded on this server" in eleven

    groq = _provider_started_message("groq", "youtube")
    assert "yt-dlp" in groq
    assert "downloading" in groq.lower()

    other = _provider_started_message("subtitles", "youtube")
    assert other.startswith("Trying subtitles")


def test_running_guidance_warns_about_stale_failed_attempts():
    guidance = _agent_guidance(
        {"status": "running", "run_id": "mcpjob_x", "progress_percent": 10},
        response_type="status",
    )
    joined = " ".join(guidance["agent_instructions"])
    assert "ALREADY tried" in joined
    assert "do not attribute" in joined
