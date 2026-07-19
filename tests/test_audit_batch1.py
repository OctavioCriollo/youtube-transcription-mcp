"""v0.4.3 audit batch 1: canonical video identity, breaker-vs-login, strict 429,
structured concurrency rejection, unbuffered worker."""

from __future__ import annotations

import subprocess
import time

from transcription_mcp import circuit_breaker, jobs
from transcription_mcp.pipeline import _cookies_newer_than_breaker
from transcription_mcp.retry_policy import ErrorClass, classify_exception
from transcription_mcp.youtube_subtitles import canonical_youtube_url


# --- P1.1: canonical video identity ----------------------------------------


def test_canonical_url_collapses_youtube_variants():
    canon = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    for variant in (
        "https://youtu.be/dQw4w9WgXcQ?si=trackingjunk",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
        "https://youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "  https://youtu.be/dQw4w9WgXcQ  ",
    ):
        assert canonical_youtube_url(variant) == canon


def test_canonical_url_passes_through_non_youtube():
    url = "https://cdn.example.com/audio/clip.mp3"
    assert canonical_youtube_url(url) == url


def test_start_job_canonicalizes_youtube_source(monkeypatch, tmp_path):
    class FakePopen:
        pid = 4321

        def __init__(self, command, **kwargs):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)

    status = jobs.start_transcription_job(
        source="https://youtu.be/dQw4w9WgXcQ?si=abc",
        source_type="youtube",
        language=None,
        workspace_dir=tmp_path,
    )
    job = jobs.read_json(tmp_path / "mcp-jobs" / status["run_id"] / "job.json")
    assert job["source"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert job["source_original"] == "https://youtu.be/dQw4w9WgXcQ?si=abc"


def test_dedup_matches_across_url_variants(monkeypatch, tmp_path):
    class FakePopen:
        pid = 4321

        def __init__(self, command, **kwargs):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)

    first = jobs.start_transcription_job(
        source="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        source_type="youtube",
        language=None,
        workspace_dir=tmp_path,
    )
    second = jobs.start_transcription_job(
        source="https://youtu.be/dQw4w9WgXcQ?si=different",
        source_type="youtube",
        language=None,
        workspace_dir=tmp_path,
    )
    assert second.get("deduplicated") is True
    assert second["run_id"] == first["run_id"]


# --- P1.2: breaker vs fresh login ------------------------------------------


def test_cookies_newer_than_breaker(tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("data", encoding="utf-8")

    # Breaker failure recorded in the past; cookies written now -> newer.
    monkeypatch.setattr(circuit_breaker, "last_failure_at", lambda ws, p: time.time() - 100)
    assert _cookies_newer_than_breaker(
        workspace_dir=tmp_path, provider="groq", cookies_file=cookies
    )

    # Failure in the future (after cookies) -> not newer.
    monkeypatch.setattr(circuit_breaker, "last_failure_at", lambda ws, p: time.time() + 100)
    assert not _cookies_newer_than_breaker(
        workspace_dir=tmp_path, provider="groq", cookies_file=cookies
    )


def test_cookies_never_override_breaker_for_elevenlabs(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("data", encoding="utf-8")
    # ElevenLabs does not use these cookies, so its breaker must stand.
    assert not _cookies_newer_than_breaker(
        workspace_dir=tmp_path, provider="elevenlabs", cookies_file=cookies
    )


def test_breaker_reset_clears_open_state_keeps_totals(tmp_path):
    for _ in range(3):
        circuit_breaker.record_blocked_failure(tmp_path, "groq")
    assert circuit_breaker.seconds_remaining(tmp_path, "groq") > 0

    circuit_breaker.reset(tmp_path, "groq")
    assert circuit_breaker.seconds_remaining(tmp_path, "groq") == 0
    snap = circuit_breaker.snapshot(tmp_path)["groq"]
    assert snap["consecutive_blocked"] == 0
    assert snap["total_failures"] == 3  # history preserved


# --- P2.5: strict 429 classification ---------------------------------------


def test_bare_429_in_byte_count_is_not_rate_limited():
    exc = RuntimeError("wrote 14290 bytes to buffer then failed: connection reset")
    # "connection reset" is transient; the stray 4290 must not read as 429.
    assert classify_exception(exc) is ErrorClass.TRANSIENT


def test_real_429_still_classified_rate_limited():
    assert classify_exception(RuntimeError("HTTP Error 429: Too Many Requests")) is (
        ErrorClass.RATE_LIMITED
    )


# --- P2.8: structured concurrency rejection --------------------------------


def test_max_concurrency_returns_structured_rejection(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "count_active_jobs", lambda **_: 2)
    monkeypatch.setattr(jobs, "_find_active_duplicate", lambda **_: None)

    result = jobs.start_transcription_job(
        source="https://youtu.be/dQw4w9WgXcQ",
        source_type="youtube",
        language=None,
        workspace_dir=tmp_path,
        max_concurrent_jobs=2,
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "max_concurrent_jobs"
    assert "run_id" not in result
    assert any("Do NOT start another" in i for i in result["agent_instructions"])
