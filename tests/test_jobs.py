from __future__ import annotations

import subprocess


def test_start_transcription_job_persists_request_and_spawns_worker(monkeypatch, tmp_path):
    from transcription_mcp import jobs

    calls = []

    class FakePopen:
        pid = 4321

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(jobs, "_is_pid_alive", lambda pid: True)

    status = jobs.start_transcription_job(
        url="https://youtu.be/example",
        language="es",
        workspace_dir=tmp_path,
    )

    run_id = status["run_id"]
    job_dir = tmp_path / "mcp-jobs" / run_id
    request = jobs.read_json(job_dir / "request.json")
    job = jobs.read_json(job_dir / "job.json")

    assert request["url"] == "https://youtu.be/example"
    assert request["language"] == "es"
    assert job["status"] == "running"
    assert job["worker_pid"] == 4321
    assert status["recommended_next_tool"] == "get_transcription_status"
    assert status["recommended_poll_seconds"] == 20
    assert status["progress_percent"] == 2
    assert "user_visible_message" in status
    assert calls
    assert calls[0][0][-3:] == ["-m", "transcription_mcp.worker", str(job_dir)]


def test_get_transcription_job_result_returns_completed_payload(tmp_path):
    from transcription_mcp import jobs

    job_dir = tmp_path / "mcp-jobs" / "mcpjob_test"
    job_dir.mkdir(parents=True)
    jobs.write_json_atomic(
        job_dir / "job.json",
        {
            "schema_version": jobs.JOB_SCHEMA_VERSION,
            "run_id": "mcpjob_test",
            "url": "https://youtu.be/example",
            "status": "completed",
            "stage": "completed",
            "message": "done",
            "result_available": True,
        },
    )
    jobs.write_json_atomic(
        job_dir / "result.json",
        {"transcript": "hola", "method": "groq"},
    )

    result = jobs.get_transcription_job_result(
        run_id="mcpjob_test",
        workspace_dir=tmp_path,
    )

    assert result["status"] == "completed"
    assert result["result_available"] is True
    assert result["result"]["transcript"] == "hola"
    assert result["progress_percent"] == 100
    assert result["recommended_next_tool"] is None
    assert "respond_with_transcript" in result["available_next_actions"]


def test_get_transcription_job_artifact_returns_named_content(tmp_path):
    from transcription_mcp import jobs

    run_dir = tmp_path / "v4-storage" / "items" / "url-test" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    artifact_path = run_dir / "subtitles.srt"
    artifact_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhola", encoding="utf-8")
    job_dir = tmp_path / "mcp-jobs" / "mcpjob_artifact"
    job_dir.mkdir(parents=True)
    jobs.write_json_atomic(
        job_dir / "job.json",
        {
            "schema_version": jobs.JOB_SCHEMA_VERSION,
            "run_id": "mcpjob_artifact",
            "url": "https://youtu.be/example",
            "status": "completed",
            "stage": "completed",
            "message": "done",
            "result_available": True,
        },
    )
    jobs.write_json_atomic(
        job_dir / "result.json",
        {
            "transcript": "hola",
            "run_dir": str(run_dir),
            "artifacts": {
                "subtitles_srt": {
                    "path": str(artifact_path),
                    "exists": True,
                    "size_bytes": artifact_path.stat().st_size,
                }
            },
        },
    )

    result = jobs.get_transcription_job_artifact(
        run_id="mcpjob_artifact",
        artifact="subtitles_srt",
        workspace_dir=tmp_path,
    )

    assert result["status"] == "completed"
    assert result["artifact"] == "subtitles_srt"
    assert "hola" in result["content"]
    assert result["recommended_next_tool"] is None
    assert "Artifact subtitles_srt is ready" in result["user_visible_message"]


def test_get_transcription_job_artifact_guides_unknown_artifact(tmp_path):
    from transcription_mcp import jobs

    run_dir = tmp_path / "v4-storage" / "items" / "url-test" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    artifact_path = run_dir / "subtitles.srt"
    artifact_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhola", encoding="utf-8")
    job_dir = tmp_path / "mcp-jobs" / "mcpjob_missing_artifact"
    job_dir.mkdir(parents=True)
    jobs.write_json_atomic(
        job_dir / "job.json",
        {
            "schema_version": jobs.JOB_SCHEMA_VERSION,
            "run_id": "mcpjob_missing_artifact",
            "url": "https://youtu.be/example",
            "status": "completed",
            "stage": "completed",
            "message": "done",
            "result_available": True,
        },
    )
    jobs.write_json_atomic(
        job_dir / "result.json",
        {
            "transcript": "hola",
            "run_dir": str(run_dir),
            "artifacts": {"subtitles_srt": {"path": str(artifact_path), "exists": True}},
        },
    )

    result = jobs.get_transcription_job_artifact(
        run_id="mcpjob_missing_artifact",
        artifact="not_real",
        workspace_dir=tmp_path,
    )

    assert result["status"] == "not_found"
    assert result["recommended_next_tool"] == "get_transcription_artifact"
    assert result["recommended_artifacts"] == ["subtitles_srt"]
    assert "Do not retry the same artifact name." in result["agent_instructions"]


def test_cancel_transcription_job_marks_job_canceled(monkeypatch, tmp_path):
    from transcription_mcp import jobs

    job_dir = tmp_path / "mcp-jobs" / "mcpjob_cancel"
    job_dir.mkdir(parents=True)
    jobs.write_json_atomic(
        job_dir / "job.json",
        {
            "schema_version": jobs.JOB_SCHEMA_VERSION,
            "run_id": "mcpjob_cancel",
            "url": "https://youtu.be/example",
            "status": "running",
            "stage": "v4_transcribing",
            "message": "running",
            "worker_pid": 1234,
        },
    )
    monkeypatch.setattr(jobs, "_terminate_process_tree", lambda pid: True)

    status = jobs.cancel_transcription_job(
        run_id="mcpjob_cancel",
        workspace_dir=tmp_path,
    )

    assert status["status"] == "canceled"
    assert status["stage"] == "canceled"


def test_create_bundle_packages_artifacts_and_rebases_path(tmp_path):
    from transcription_mcp.bundle import create_bundle

    run = tmp_path / "v4-storage" / "items" / "url-x" / "runs" / "run_1"
    run.mkdir(parents=True)
    (run / "transcript.txt").write_text("hola", encoding="utf-8")
    (run / "subtitles.srt").write_text("1\n", encoding="utf-8")
    (run / "run.json").write_text("{}", encoding="utf-8")

    meta = create_bundle(
        run_dir=run,
        workspace_dir=tmp_path,
        openclaw_workspace_dir="/home/node/.openclaw/mcp-workspace/transcription-mcp",
        ttl_hours=24,
    )

    assert meta["status"] == "completed"
    assert set(meta["included_artifacts"]) == {"transcript.txt", "subtitles.srt", "run.json"}
    assert meta["size_bytes"] > 0
    assert len(meta["sha256"]) == 64
    assert (run / "exports" / "transcription_bundle.zip").is_file()
    # path is rebased from the MCP workspace to the OpenClaw read-only mount
    assert meta["bundle_path_for_openclaw"].startswith(
        "/home/node/.openclaw/mcp-workspace/transcription-mcp/"
    )
    assert meta["bundle_path_for_openclaw"].endswith(
        "runs/run_1/exports/transcription_bundle.zip"
    )


def test_create_bundle_raises_when_no_artifacts(tmp_path):
    from transcription_mcp.bundle import BundleError, create_bundle

    run = tmp_path / "runs" / "empty"
    run.mkdir(parents=True)
    try:
        create_bundle(run_dir=run, workspace_dir=tmp_path)
        assert False, "expected BundleError"
    except BundleError:
        pass


def test_is_job_stale_detects_old_heartbeat(monkeypatch):
    from transcription_mcp import jobs

    monkeypatch.setattr(jobs, "JOB_STALE_SECONDS", 180.0)
    fresh = {"status": "running", "heartbeat_at": jobs._now_iso()}
    assert jobs._is_job_stale(fresh) is False

    old = {"status": "running", "heartbeat_at": "2000-01-01T00:00:00+00:00"}
    assert jobs._is_job_stale(old) is True

    terminal = {"status": "completed", "heartbeat_at": "2000-01-01T00:00:00+00:00"}
    assert jobs._is_job_stale(terminal) is False
