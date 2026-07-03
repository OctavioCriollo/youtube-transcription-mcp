"""Tests for corrective item 3: error classification + bounded same-tier retries."""

from __future__ import annotations

import pytest

from transcription_mcp.retry_policy import (
    ErrorClass,
    backoff_seconds,
    classify_exception,
    max_retries_for,
)


class TestClassification:
    def test_youtube_403_is_blocked(self):
        exc = RuntimeError("yt-dlp failed to download audio: HTTP Error 403: Forbidden")
        assert classify_exception(exc) is ErrorClass.BLOCKED

    def test_bot_check_is_blocked(self):
        exc = RuntimeError("Sign in to confirm you're not a bot")
        assert classify_exception(exc) is ErrorClass.BLOCKED

    def test_groq_429_is_rate_limited(self):
        exc = RuntimeError("Groq API returned HTTP 429: Too Many Requests")
        assert classify_exception(exc) is ErrorClass.RATE_LIMITED

    def test_quota_is_rate_limited(self):
        exc = RuntimeError("quota exceeded for this billing period")
        assert classify_exception(exc) is ErrorClass.RATE_LIMITED

    def test_timeout_is_transient(self):
        exc = RuntimeError("The read operation timed out")
        assert classify_exception(exc) is ErrorClass.TRANSIENT

    def test_connection_reset_is_transient(self):
        exc = RuntimeError("Connection reset by peer")
        assert classify_exception(exc) is ErrorClass.TRANSIENT

    def test_bad_gateway_is_transient(self):
        exc = RuntimeError("HTTP Error 502: Bad Gateway")
        assert classify_exception(exc) is ErrorClass.TRANSIENT

    def test_unknown_is_fatal(self):
        exc = ValueError("url must not be empty")
        assert classify_exception(exc) is ErrorClass.FATAL

    def test_cause_chain_is_inspected(self):
        root = TimeoutError("timed out")
        wrapper = RuntimeError("yt-dlp failed to download audio")
        wrapper.__cause__ = root
        assert classify_exception(wrapper) is ErrorClass.TRANSIENT

    def test_rate_limit_wins_over_transient_words(self):
        exc = RuntimeError("HTTP Error 429 while reading from server: timed out")
        assert classify_exception(exc) is ErrorClass.RATE_LIMITED


class TestPolicy:
    def test_blocked_earns_no_retries(self):
        assert max_retries_for(ErrorClass.BLOCKED) == 0

    def test_fatal_earns_no_retries(self):
        assert max_retries_for(ErrorClass.FATAL) == 0

    def test_transient_default_retries(self, monkeypatch):
        monkeypatch.delenv("MCP_RETRY_TRANSIENT", raising=False)
        assert max_retries_for(ErrorClass.TRANSIENT) == 2

    def test_retries_env_override(self, monkeypatch):
        monkeypatch.setenv("MCP_RETRY_TRANSIENT", "5")
        assert max_retries_for(ErrorClass.TRANSIENT) == 5

    def test_negative_env_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv("MCP_RETRY_RATE_LIMITED", "-3")
        assert max_retries_for(ErrorClass.RATE_LIMITED) == 0

    def test_rate_limit_backoff_longer_than_transient(self):
        assert backoff_seconds(ErrorClass.RATE_LIMITED, 1) > backoff_seconds(
            ErrorClass.TRANSIENT, 1
        )

    def test_backoff_is_bounded(self):
        assert backoff_seconds(ErrorClass.RATE_LIMITED, 10) <= 61.5
        assert backoff_seconds(ErrorClass.TRANSIENT, 10) <= 11.5


class TestChainBehavior:
    """The chain retries transient errors on the same tier, escalates blocked ones."""

    @pytest.fixture()
    def chain_env(self, monkeypatch, tmp_path):
        from transcription_mcp import pipeline as mcp_pipeline

        monkeypatch.setattr(mcp_pipeline, "_retry_sleep", lambda seconds: None)
        return mcp_pipeline, tmp_path

    def test_transient_error_retries_same_tier_then_succeeds(self, monkeypatch, chain_env):
        mcp_pipeline, tmp_path = chain_env
        calls: list[str] = []

        def fake_engine(url, *, provider, **kwargs):
            calls.append(provider)
            if len(calls) < 2:
                raise RuntimeError("Connection reset by peer")
            run_dir = tmp_path / "run"
            run_dir.mkdir(exist_ok=True)
            return run_dir

        monkeypatch.setattr(mcp_pipeline, "engine_transcribe_youtube", fake_engine)
        monkeypatch.setattr(
            mcp_pipeline, "_successful_result", lambda **kwargs: {"method": kwargs["method"]}
        )

        result = mcp_pipeline._transcribe_url_chain(
            url="https://youtube.com/watch?v=x",
            source_kind="youtube",
            workspace_dir=tmp_path,
            providers=("groq", "elevenlabs"),
            language=None,
            diarize=False,
            num_speakers=None,
            ytdlp_cookies_file=None,
            ytdlp_proxy=None,
            allow_subtitles=False,
            status_callback=None,
        )
        # Same tier retried: groq twice, elevenlabs never touched.
        assert calls == ["groq", "groq"]
        assert result["method"] == "groq"

    def test_blocked_error_escalates_immediately(self, monkeypatch, chain_env):
        mcp_pipeline, tmp_path = chain_env
        calls: list[str] = []

        def fake_engine(url, *, provider, **kwargs):
            calls.append(provider)
            if provider == "groq":
                raise RuntimeError("HTTP Error 403: Forbidden")
            run_dir = tmp_path / "run"
            run_dir.mkdir(exist_ok=True)
            return run_dir

        monkeypatch.setattr(mcp_pipeline, "engine_transcribe_youtube", fake_engine)
        monkeypatch.setattr(
            mcp_pipeline, "_successful_result", lambda **kwargs: {"method": kwargs["method"]}
        )

        result = mcp_pipeline._transcribe_url_chain(
            url="https://youtube.com/watch?v=x",
            source_kind="youtube",
            workspace_dir=tmp_path,
            providers=("groq", "elevenlabs"),
            language=None,
            diarize=False,
            num_speakers=None,
            ytdlp_cookies_file=None,
            ytdlp_proxy=None,
            allow_subtitles=False,
            status_callback=None,
        )
        # No same-tier retry for blocked: one groq attempt, straight to elevenlabs.
        assert calls == ["groq", "elevenlabs"]
        assert result["method"] == "elevenlabs"
