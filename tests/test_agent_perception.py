"""Fixes for the agent-perception failure mode (v0.3.0).

A real session (2026-07-04) showed the failure chain: during a long provider
stage the status looked frozen, the agent decided the job was stuck, called the
synchronous tool as a "fallback", hit the client-side timeout, and paid
ElevenLabs twice for the same 31-minute video - starting 2 seconds AFTER the
original job had already completed. These tests pin the four countermeasures:

- liveness signals (elapsed_seconds / heartbeat_age_seconds) in status payloads
- an explicit "quiet != stuck" note on unchanged watch long-polls
- dedup: start_transcription_job reuses an identical ACTIVE job
- sync tools hand off to watch_transcription instead of blocking past budget
- abandoned engine run-state is finalized as failed, not left "running"
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta


def _write_job(
    workspace,
    run_id,
    *,
    status="running",
    stage="engine_transcribing",
    revision=3,
    source="https://youtu.be/livetest",
    source_type="youtube",
    language=None,
    diarize=False,
    num_speakers=None,
    started_ago_s=95.0,
    heartbeat_ago_s=2.0,
):
    job_dir = workspace / "mcp-jobs" / run_id
    job_dir.mkdir(parents=True)
    now = datetime.now(UTC)
    job = {
        "schema_version": "mcp-transcription-job-v1",
        "run_id": run_id,
        "source": source,
        "source_type": source_type,
        "url": source if source_type in {"youtube", "media_url"} else None,
        "language": language,
        "diarize": diarize,
        "num_speakers": num_speakers,
        "status": status,
        "stage": stage,
        "message": "test",
        "progress": 0.5,
        "revision": revision,
        "created_at": (now - timedelta(seconds=started_ago_s)).isoformat(),
        "updated_at": now.isoformat(),
        "started_at": (now - timedelta(seconds=started_ago_s)).isoformat(),
        "heartbeat_at": (now - timedelta(seconds=heartbeat_ago_s)).isoformat(),
        "result_available": False,
        "logs": {},
    }
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return job_dir


# --- liveness signals -------------------------------------------------------


def test_running_status_reports_liveness_and_patience(tmp_path):
    from transcription_mcp.jobs import get_transcription_job_status

    _write_job(tmp_path, "mcpjob_live", started_ago_s=95.0, heartbeat_ago_s=2.0)
    status = get_transcription_job_status(run_id="mcpjob_live", workspace_dir=tmp_path)

    assert status["elapsed_seconds"] >= 94
    assert status["heartbeat_age_seconds"] <= 5
    message = status["user_visible_message"]
    assert "alive" in message
    assert "keep watching" in message
    assert status["recommended_next_tool"] == "watch_transcription"
    assert any("Do NOT start another" in item for item in status["agent_instructions"])


def test_terminal_status_has_no_liveness_fields(tmp_path):
    from transcription_mcp.jobs import get_transcription_job_status

    _write_job(tmp_path, "mcpjob_done", status="completed", stage="completed")
    status = get_transcription_job_status(run_id="mcpjob_done", workspace_dir=tmp_path)

    assert "elapsed_seconds" not in status
    assert "heartbeat_age_seconds" not in status


def test_unchanged_watch_carries_quiet_is_not_stuck_note(tmp_path):
    from transcription_mcp.jobs import watch_transcription_job

    _write_job(tmp_path, "mcpjob_quiet", revision=4)
    result = asyncio.run(
        watch_transcription_job(
            run_id="mcpjob_quiet",
            workspace_dir=tmp_path,
            since_revision=4,
            timeout_seconds=1,
        )
    )

    assert result["changed"] is False
    assert "note" in result
    assert "do NOT start a duplicate" in result["note"]
    assert "alive" in result["note"]


# --- dedup guard ------------------------------------------------------------


def test_start_reuses_identical_active_job(tmp_path):
    from transcription_mcp import jobs

    existing = _write_job(tmp_path, "mcpjob_20260101T000000_orig")
    status = jobs.start_transcription_job(
        source="https://youtu.be/livetest",
        source_type="youtube",
        language=None,
        workspace_dir=tmp_path,
    )

    assert status["deduplicated"] is True
    assert status["run_id"] == "mcpjob_20260101T000000_orig"
    assert "already active" in status["user_visible_message"]
    # No second job dir was created.
    assert sorted(p.name for p in (tmp_path / "mcp-jobs").iterdir()) == [existing.name]


def test_start_does_not_dedup_different_options(tmp_path, monkeypatch):
    import subprocess

    from transcription_mcp import jobs

    class FakePopen:
        pid = 4321

        def __init__(self, command, **kwargs):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)

    _write_job(tmp_path, "mcpjob_20260101T000000_orig", diarize=False)
    status = jobs.start_transcription_job(
        source="https://youtu.be/livetest",
        source_type="youtube",
        language=None,
        diarize=True,  # different request -> NOT a duplicate
        workspace_dir=tmp_path,
        max_concurrent_jobs=5,
    )

    assert "deduplicated" not in status
    assert status["run_id"] != "mcpjob_20260101T000000_orig"


def test_start_ignores_terminal_jobs_for_dedup(tmp_path, monkeypatch):
    import subprocess

    from transcription_mcp import jobs

    class FakePopen:
        pid = 4321

        def __init__(self, command, **kwargs):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)

    _write_job(tmp_path, "mcpjob_20260101T000000_done", status="completed", stage="completed")
    status = jobs.start_transcription_job(
        source="https://youtu.be/livetest",
        source_type="youtube",
        language=None,
        workspace_dir=tmp_path,
    )

    assert "deduplicated" not in status


# --- sync budget handoff ----------------------------------------------------


def test_budget_runner_hands_off_instead_of_blocking(tmp_path):
    from transcription_mcp.jobs import run_transcription_job_with_budget

    # An identical active job exists, so the runner dedups onto it and then
    # watches until the (tiny) budget runs out -> handoff, not an error.
    _write_job(tmp_path, "mcpjob_20260101T000000_orig")
    result = asyncio.run(
        run_transcription_job_with_budget(
            budget_seconds=5.0,
            workspace_dir=tmp_path,
            source="https://youtu.be/livetest",
            source_type="youtube",
            language=None,
        )
    )

    assert result["sync_budget_exceeded"] is True
    assert result["run_id"] == "mcpjob_20260101T000000_orig"
    assert result["recommended_next_tool"] == "watch_transcription"
    assert "not a failure" in " ".join(result["agent_instructions"])


def test_budget_runner_returns_result_when_job_completes(tmp_path, monkeypatch):
    from transcription_mcp import jobs

    job_dir = _write_job(
        tmp_path, "mcpjob_20260101T000000_fast", status="completed", stage="completed"
    )
    (job_dir / "result.json").write_text(
        json.dumps({"transcript": "hola", "method": "groq"}), encoding="utf-8"
    )

    def fake_start(**kwargs):
        return jobs.get_transcription_job_status(
            run_id="mcpjob_20260101T000000_fast", workspace_dir=tmp_path
        )

    monkeypatch.setattr(jobs, "start_transcription_job", fake_start)
    result = asyncio.run(
        jobs.run_transcription_job_with_budget(
            budget_seconds=5.0,
            workspace_dir=tmp_path,
            source="https://youtu.be/livetest",
            source_type="youtube",
            language=None,
        )
    )

    assert result["status"] == "completed"
    assert result["result_available"] is True
    assert result["result"]["transcript"] == "hola"


# --- abandoned engine run-state hygiene --------------------------------------


def test_failed_provider_attempt_finalizes_abandoned_run_state(tmp_path):
    from transcription_engine.storage import item_id_for_url
    from transcription_mcp.pipeline import _mark_abandoned_engine_run

    url = "https://youtu.be/abandoned"
    run_dir = (
        tmp_path / "storage" / "items" / item_id_for_url(url) / "runs" / "run_x"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "run-state.json").write_text(
        json.dumps({"provider": "groq", "source_url": url, "status": "running"}),
        encoding="utf-8",
    )

    _mark_abandoned_engine_run(
        workspace_dir=tmp_path,
        source_url=url,
        provider="groq",
        error="[blocked] boom",
    )

    state = json.loads((run_dir / "run-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["error"] == "[blocked] boom"


def test_finalized_runs_are_never_touched(tmp_path):
    from transcription_engine.storage import item_id_for_url
    from transcription_mcp.pipeline import _mark_abandoned_engine_run

    url = "https://youtu.be/finalized"
    run_dir = (
        tmp_path / "storage" / "items" / item_id_for_url(url) / "runs" / "run_ok"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    (run_dir / "run-state.json").write_text(
        json.dumps({"provider": "groq", "source_url": url, "status": "running"}),
        encoding="utf-8",
    )

    _mark_abandoned_engine_run(
        workspace_dir=tmp_path,
        source_url=url,
        provider="groq",
        error="[blocked] boom",
    )

    state = json.loads((run_dir / "run-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "running"  # untouched: run.json marks it finalized
