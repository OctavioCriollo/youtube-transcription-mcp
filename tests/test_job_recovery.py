"""Regression tests for the 2026-07-08 'completed on disk but marked stale' bug.

Root cause (audited): a job whose worker wrote result.json but died/hung/was
PID-reused before flipping job.json to 'completed' was declared stale_failed
180s later, and get_transcription_result withheld the finished transcript.

The fixes make disk authoritative (a complete result.json always wins, checked
before any pid/heartbeat logic) and serialize job.json writes so a heartbeat
cannot clobber the completed flip.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from transcription_mcp import jobs


def _make_job(
    workspace,
    run_id,
    *,
    status="running",
    stage="engine_transcribing",
    heartbeat_ago_s=0.0,
    worker_pid=4321,
    with_result=False,
    with_error=False,
    source="https://youtu.be/x",
):
    job_dir = workspace / "mcp-jobs" / run_id
    job_dir.mkdir(parents=True)
    now = datetime.now(UTC)
    job = {
        "schema_version": jobs.JOB_SCHEMA_VERSION,
        "run_id": run_id,
        "source": source,
        "source_type": "youtube",
        "url": source,
        "status": status,
        "stage": stage,
        "message": "test",
        "progress": 0.5,
        "revision": 3,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "started_at": (now - timedelta(seconds=max(heartbeat_ago_s, 1))).isoformat(),
        "heartbeat_at": (now - timedelta(seconds=heartbeat_ago_s)).isoformat(),
        "worker_pid": worker_pid,
        "result_available": False,
        "logs": {},
        "workspace_dir": str(workspace),
    }
    jobs.write_json_atomic(job_dir / "job.json", job)
    if with_result:
        jobs.write_json_atomic(
            job_dir / "result.json", {"transcript": "hola", "method": "elevenlabs"}
        )
    if with_error:
        jobs.write_json_atomic(
            job_dir / "error.json", {"message": "boom", "type": "RuntimeError"}
        )
    return job_dir


# --- artifact-authoritative recovery ---------------------------------------


def test_status_recovers_completed_when_result_on_disk_and_pid_alive(tmp_path, monkeypatch):
    # The exact bug: pid looks alive (hung / PID-reused), heartbeat ancient (would
    # be stale), but result.json is complete. Must recover to completed, NOT stale.
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)
    _make_job(tmp_path, "mcpjob_r1", heartbeat_ago_s=9999, with_result=True)
    status = jobs.get_transcription_job_status(run_id="mcpjob_r1", workspace_dir=tmp_path)
    assert status["status"] == "completed"
    assert status["result_available"] is True


def test_result_served_from_disk_when_status_stuck_running(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)
    _make_job(tmp_path, "mcpjob_r2", status="running", heartbeat_ago_s=9999, with_result=True)
    res = jobs.get_transcription_job_result(run_id="mcpjob_r2", workspace_dir=tmp_path)
    assert res["status"] == "completed"
    assert res["result_available"] is True
    assert res["result"]["transcript"] == "hola"


def test_dead_worker_with_result_still_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: False)
    _make_job(tmp_path, "mcpjob_r3", with_result=True)
    status = jobs.get_transcription_job_status(run_id="mcpjob_r3", workspace_dir=tmp_path)
    assert status["status"] == "completed"


# --- guardrails: don't over-recover ----------------------------------------


def test_stale_without_result_still_fails(tmp_path, monkeypatch):
    # No result on disk + ancient heartbeat + pid "alive" -> genuinely stale.
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)
    _make_job(tmp_path, "mcpjob_r4", heartbeat_ago_s=9999, with_result=False)
    status = jobs.get_transcription_job_status(run_id="mcpjob_r4", workspace_dir=tmp_path)
    assert status["status"] == "stale_failed"


def test_dead_worker_with_error_and_no_result_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: False)
    _make_job(tmp_path, "mcpjob_r5", with_result=False, with_error=True)
    status = jobs.get_transcription_job_status(run_id="mcpjob_r5", workspace_dir=tmp_path)
    assert status["status"] == "failed"


# --- write serialization (race F4) -----------------------------------------


def test_completed_flip_survives_concurrent_heartbeats(tmp_path):
    # With locked read-modify-write, once we flip to completed, concurrent
    # heartbeat writers re-read completed and preserve it — they can never
    # rewrite the whole file with a stale status=running.
    job_dir = _make_job(tmp_path, "mcpjob_flip", status="running")
    stop = threading.Event()

    def spam():
        while not stop.is_set():
            jobs.update_job_status(job_dir, heartbeat_at=jobs._now_iso())

    threads = [threading.Thread(target=spam) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.05)
        jobs.update_job_status(job_dir, status="completed", stage="completed")
        time.sleep(0.1)  # give the spammers a chance to (wrongly) revert it
        assert jobs.read_json(job_dir / "job.json")["status"] == "completed"
    finally:
        stop.set()
        for t in threads:
            t.join()
    # No stray per-writer temp files left behind.
    assert not list(job_dir.glob("job.json*.tmp"))


def test_write_json_atomic_leaves_no_tmp(tmp_path):
    path = tmp_path / "x.json"
    jobs.write_json_atomic(path, {"a": 1})
    jobs.write_json_atomic(path, {"a": 2})
    assert jobs.read_json(path) == {"a": 2}
    assert not list(tmp_path.glob("x.json*.tmp"))
