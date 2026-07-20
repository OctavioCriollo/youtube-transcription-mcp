"""v0.4.4: the login hint must survive breaker skips and reach running status.

Two gaps found live (job mcpjob_20260719T055034): (a) when the breaker skips
groq, the failed_attempts reason says "[breaker_open]" without the bot-wall
signature, so the result-level hint never fired for any job inside the
cooldown window; (b) the hint only traveled on final results, so the agent
could not offer the login WHILE the slow fallback tier was still running -
which is exactly the best moment to do it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from transcription_mcp.jobs import (
    _login_hint_from_failed_attempts,
    get_transcription_job_status,
)
from transcription_mcp.pipeline import _youtube_login_hint

SIGN_IN = (
    "[blocked] YoutubeDownloadError: yt-dlp failed: ERROR: Sign in to confirm "
    "you're not a bot."
)
BREAKER = "[breaker_open] Skipped after repeated blocked failures; cooldown 280s remaining."


# --- gap A: breaker_open must fire the result-level hint --------------------


def test_result_hint_fires_when_breaker_skipped_groq():
    hint = _youtube_login_hint({"groq": BREAKER}, cookies_in_effect=False)
    assert hint["youtube_login_would_help"] is True


def test_result_hint_still_absent_for_unrelated_failures():
    assert _youtube_login_hint({"groq": "[transient] timeout"}, cookies_in_effect=False) == {}


# --- gap B: hint in status payloads while running ---------------------------


def test_status_hint_matches_blocked_and_breaker_reasons():
    assert _login_hint_from_failed_attempts({"groq": SIGN_IN})["youtube_login_would_help"]
    assert _login_hint_from_failed_attempts({"groq": BREAKER})["youtube_login_would_help"]
    assert _login_hint_from_failed_attempts({"groq": "[fatal] boom"}) == {}
    assert _login_hint_from_failed_attempts(None) == {}


def test_running_job_status_carries_login_hint(tmp_path):
    job_dir = tmp_path / "mcp-jobs" / "mcpjob_hint"
    job_dir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "schema_version": "mcp-transcription-job-v1",
                "run_id": "mcpjob_hint",
                "source": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "source_type": "youtube",
                "status": "running",
                "stage": "engine_initialized",
                "message": "running",
                "progress": 0.1,
                "revision": 2,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "heartbeat_at": now,
                "failed_attempts": {"groq": SIGN_IN},
                "result_available": False,
                "logs": {},
            }
        ),
        encoding="utf-8",
    )

    status = get_transcription_job_status(run_id="mcpjob_hint", workspace_dir=tmp_path)
    assert status["youtube_login_would_help"] is True
    assert "request_youtube_login" in status["youtube_login_message"]
    # Still a normal running payload otherwise.
    assert status["status"] == "running"
    assert status["recommended_next_tool"] == "watch_transcription"
