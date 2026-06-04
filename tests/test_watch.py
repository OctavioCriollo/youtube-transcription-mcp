"""Corrective 13: watch_transcription long-poll + monotonic revision."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime


def _make_job(workspace, run_id, *, status, stage, revision, worker_pid=None):
    job_dir = workspace / "mcp-jobs" / run_id
    job_dir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    job = {
        "schema_version": "mcp-transcription-job-v1",
        "run_id": run_id,
        "source": "https://youtu.be/x",
        "source_type": "youtube",
        "url": "https://youtu.be/x",
        "status": status,
        "stage": stage,
        "message": "test",
        "progress": 0.5,
        "revision": revision,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "heartbeat_at": now,  # recent -> not stale
        "result_available": False,
        "logs": {},
    }
    if worker_pid is not None:
        job["worker_pid"] = worker_pid
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return job_dir


def test_watch_returns_immediately_when_terminal(tmp_path):
    from transcription_mcp.jobs import watch_transcription_job

    _make_job(tmp_path, "mcpjob_term", status="completed", stage="completed", revision=5)
    res = asyncio.run(
        watch_transcription_job(
            run_id="mcpjob_term", workspace_dir=tmp_path, since_revision=5, timeout_seconds=10
        )
    )
    assert res["terminal"] is True
    assert res["changed"] is True
    assert res["status"] == "completed"


def test_watch_returns_on_revision_change(tmp_path):
    from transcription_mcp.jobs import watch_transcription_job

    _make_job(tmp_path, "mcpjob_chg", status="running", stage="transcribing", revision=3)
    res = asyncio.run(
        watch_transcription_job(
            run_id="mcpjob_chg", workspace_dir=tmp_path, since_revision=2, timeout_seconds=10
        )
    )
    assert res["changed"] is True
    assert res["terminal"] is False
    assert res["revision"] == 3
    assert res["recommended_next_tool"] == "watch_transcription"


def test_watch_blocks_until_timeout_when_no_change(tmp_path):
    from transcription_mcp.jobs import watch_transcription_job

    _make_job(tmp_path, "mcpjob_block", status="running", stage="transcribing", revision=4)
    start = time.monotonic()
    res = asyncio.run(
        watch_transcription_job(
            run_id="mcpjob_block", workspace_dir=tmp_path, since_revision=4, timeout_seconds=1
        )
    )
    elapsed = time.monotonic() - start
    assert res["changed"] is False
    assert res["terminal"] is False
    assert elapsed >= 1.0


def test_revision_bumps_on_stage_change_not_on_heartbeat(tmp_path):
    from transcription_mcp.jobs import update_job_status

    job_dir = _make_job(tmp_path, "mcpjob_rev", status="running", stage="a", revision=1)
    # heartbeat-only update -> NO revision bump
    j1 = update_job_status(job_dir, heartbeat_at="2026-01-01T00:00:00Z")
    assert j1["revision"] == 1
    # stage change -> bump
    j2 = update_job_status(job_dir, stage="b")
    assert j2["revision"] == 2
    # status change -> bump
    j3 = update_job_status(job_dir, status="completed")
    assert j3["revision"] == 3
