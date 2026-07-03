"""Tests for corrective item 4: per-provider circuit breaker."""

from __future__ import annotations

import pytest

from transcription_mcp import circuit_breaker as cb


class TestBreakerCore:
    def test_closed_by_default(self, tmp_path):
        assert cb.seconds_remaining(tmp_path, "groq") == 0.0

    def test_opens_after_threshold_blocked_failures(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "3")
        monkeypatch.setenv("MCP_BREAKER_COOLDOWN_S", "300")
        assert cb.record_blocked_failure(tmp_path, "groq") == 0.0
        assert cb.record_blocked_failure(tmp_path, "groq") == 0.0
        assert cb.record_blocked_failure(tmp_path, "groq") == 300.0
        assert cb.seconds_remaining(tmp_path, "groq") > 0

    def test_success_resets_and_closes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "2")
        cb.record_blocked_failure(tmp_path, "groq")
        cb.record_blocked_failure(tmp_path, "groq")
        assert cb.seconds_remaining(tmp_path, "groq") > 0
        cb.record_success(tmp_path, "groq")
        assert cb.seconds_remaining(tmp_path, "groq") == 0.0
        snap = cb.snapshot(tmp_path)["groq"]
        assert snap["consecutive_blocked"] == 0
        assert snap["total_successes"] == 1

    def test_other_failures_never_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "1")
        for _ in range(5):
            cb.record_other_failure(tmp_path, "groq")
        assert cb.seconds_remaining(tmp_path, "groq") == 0.0
        assert cb.snapshot(tmp_path)["groq"]["total_failures"] == 5

    def test_providers_are_independent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "1")
        cb.record_blocked_failure(tmp_path, "groq")
        assert cb.seconds_remaining(tmp_path, "groq") > 0
        assert cb.seconds_remaining(tmp_path, "elevenlabs") == 0.0

    def test_disabled_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_BREAKER_ENABLED", "0")
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "1")
        cb.record_blocked_failure(tmp_path, "groq")
        assert cb.seconds_remaining(tmp_path, "groq") == 0.0

    def test_corrupt_state_file_is_tolerated(self, tmp_path):
        (tmp_path / "circuit_breaker.json").write_text("{not json", encoding="utf-8")
        assert cb.seconds_remaining(tmp_path, "groq") == 0.0
        cb.record_success(tmp_path, "groq")
        assert cb.snapshot(tmp_path)["groq"]["total_successes"] == 1


class TestChainIntegration:
    @pytest.fixture()
    def chain_env(self, monkeypatch, tmp_path):
        from transcription_mcp import pipeline as mcp_pipeline

        monkeypatch.setattr(mcp_pipeline, "_retry_sleep", lambda seconds: None)
        monkeypatch.setattr(
            mcp_pipeline,
            "_successful_result",
            lambda **kwargs: {
                "method": kwargs["method"],
                "failed_attempts": kwargs.get("failed_attempts") or {},
            },
        )
        return mcp_pipeline, tmp_path

    def _run_chain(self, mcp_pipeline, tmp_path):
        return mcp_pipeline._transcribe_url_chain(
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

    def test_open_breaker_skips_provider_without_calling_it(
        self, monkeypatch, chain_env
    ):
        mcp_pipeline, tmp_path = chain_env
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "1")
        cb.record_blocked_failure(tmp_path, "groq")  # opens groq

        calls: list[str] = []

        def fake_engine(url, *, provider, **kwargs):
            calls.append(provider)
            run_dir = tmp_path / "run"
            run_dir.mkdir(exist_ok=True)
            return run_dir

        monkeypatch.setattr(mcp_pipeline, "engine_transcribe_youtube", fake_engine)
        result = self._run_chain(mcp_pipeline, tmp_path)
        assert calls == ["elevenlabs"]
        assert result["method"] == "elevenlabs"
        assert "breaker_open" in result["failed_attempts"]["groq"]

    def test_blocked_failures_in_chain_open_the_breaker(self, monkeypatch, chain_env):
        mcp_pipeline, tmp_path = chain_env
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "2")

        def fake_engine(url, *, provider, **kwargs):
            if provider == "groq":
                raise RuntimeError("HTTP Error 403: Forbidden")
            run_dir = tmp_path / "run"
            run_dir.mkdir(exist_ok=True)
            return run_dir

        monkeypatch.setattr(mcp_pipeline, "engine_transcribe_youtube", fake_engine)
        self._run_chain(mcp_pipeline, tmp_path)  # blocked failure 1
        assert cb.seconds_remaining(tmp_path, "groq") == 0.0
        self._run_chain(mcp_pipeline, tmp_path)  # blocked failure 2 -> opens
        assert cb.seconds_remaining(tmp_path, "groq") > 0

    def test_success_closes_breaker_for_next_jobs(self, monkeypatch, chain_env):
        mcp_pipeline, tmp_path = chain_env
        monkeypatch.setenv("MCP_BREAKER_THRESHOLD", "1")
        cb.record_blocked_failure(tmp_path, "groq")

        # Cooldown expired scenario: force-close by manipulating time via success
        cb.record_success(tmp_path, "groq")

        calls: list[str] = []

        def fake_engine(url, *, provider, **kwargs):
            calls.append(provider)
            run_dir = tmp_path / "run"
            run_dir.mkdir(exist_ok=True)
            return run_dir

        monkeypatch.setattr(mcp_pipeline, "engine_transcribe_youtube", fake_engine)
        result = self._run_chain(mcp_pipeline, tmp_path)
        assert calls == ["groq"]
        assert result["method"] == "groq"
