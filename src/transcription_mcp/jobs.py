"""Persistent MCP job control for long-running transcriptions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcription_v4.status import inspect_run
from transcription_v4.storage import item_id_for_url

JOB_SCHEMA_VERSION = "mcp-transcription-job-v1"
TERMINAL_STATUSES = {"completed", "failed", "canceled"}
SAFE_RUN_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


class JobNotFoundError(FileNotFoundError):
    """Requested job id does not exist in the MCP job store."""


def start_transcription_job(
    *,
    url: str,
    language: str | None,
    workspace_dir: Path,
) -> dict[str, Any]:
    url = str(url).strip()
    if not url:
        raise ValueError("url must not be empty")

    job_dir = _new_job_dir(workspace_dir)
    run_id = job_dir.name
    created_at = _now_iso()
    logs = {
        "stdout": str(job_dir / "worker.stdout.log"),
        "stderr": str(job_dir / "worker.stderr.log"),
    }
    request = {
        "schema_version": JOB_SCHEMA_VERSION,
        "run_id": run_id,
        "url": url,
        "language": language,
        "workspace_dir": str(Path(workspace_dir)),
    }
    job = {
        "schema_version": JOB_SCHEMA_VERSION,
        "run_id": run_id,
        "url": url,
        "language": language,
        "status": "queued",
        "stage": "queued",
        "message": "Transcription job queued.",
        "progress": 0.0,
        "created_at": created_at,
        "updated_at": created_at,
        "result_available": False,
        "logs": logs,
    }
    write_json_atomic(job_dir / "request.json", request)
    write_json_atomic(job_dir / "job.json", job)

    command = [sys.executable, "-m", "transcription_mcp.worker", str(job_dir)]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    with (job_dir / "worker.stdout.log").open("ab") as stdout, (
        job_dir / "worker.stderr.log"
    ).open("ab") as stderr:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=False if os.name == "nt" else True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )

    update_job_status(
        job_dir,
        status="running",
        stage="worker_started",
        message="Worker process started.",
        progress=0.02,
        worker_pid=process.pid,
        started_at=_now_iso(),
    )
    return get_transcription_job_status(run_id=run_id, workspace_dir=workspace_dir)


def get_transcription_job_status(
    *,
    run_id: str,
    workspace_dir: Path,
) -> dict[str, Any]:
    job_dir = get_job_dir(workspace_dir=workspace_dir, run_id=run_id)
    job = _read_job(job_dir)
    job = _refresh_job_status(job_dir, job)
    return _public_job(job_dir, job)


def get_transcription_job_result(
    *,
    run_id: str,
    workspace_dir: Path,
) -> dict[str, Any]:
    status = get_transcription_job_status(run_id=run_id, workspace_dir=workspace_dir)
    job_dir = get_job_dir(workspace_dir=workspace_dir, run_id=run_id)
    if status["status"] != "completed":
        response = {
            "run_id": run_id,
            "status": status["status"],
            "stage": status.get("stage"),
            "message": status.get("message"),
            "result_available": False,
        }
        if status["status"] == "failed":
            response["error"] = _read_json_optional(job_dir / "error.json")
        return response

    result_path = job_dir / "result.json"
    if not result_path.exists():
        update_job_status(
            job_dir,
            status="failed",
            stage="missing_result",
            message="Job is completed but result.json is missing.",
            error="result.json is missing",
        )
        return get_transcription_job_result(run_id=run_id, workspace_dir=workspace_dir)

    return {
        "run_id": run_id,
        "status": "completed",
        "result_available": True,
        "result": read_json(result_path),
    }


def cancel_transcription_job(
    *,
    run_id: str,
    workspace_dir: Path,
) -> dict[str, Any]:
    job_dir = get_job_dir(workspace_dir=workspace_dir, run_id=run_id)
    job = _read_job(job_dir)
    if job.get("status") in TERMINAL_STATUSES:
        return _public_job(job_dir, job)

    pid = _int_or_none(job.get("worker_pid"))
    update_job_status(
        job_dir,
        status="canceling",
        stage="cancel_requested",
        message="Cancellation requested.",
        cancel_requested=True,
    )
    terminated = _terminate_process_tree(pid) if pid else False
    update_job_status(
        job_dir,
        status="canceled",
        stage="canceled",
        message="Job canceled." if terminated else "Job marked canceled.",
        progress=1.0,
        finished_at=_now_iso(),
    )
    return get_transcription_job_status(run_id=run_id, workspace_dir=workspace_dir)


def get_job_dir(*, workspace_dir: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    job_dir = Path(workspace_dir) / "mcp-jobs" / run_id
    if not job_dir.is_dir():
        raise JobNotFoundError(run_id)
    return job_dir


def update_job_status(job_dir: Path, **updates: Any) -> dict[str, Any]:
    job_path = Path(job_dir) / "job.json"
    job = read_json(job_path) if job_path.exists() else {}
    job.update({key: value for key, value in updates.items() if value is not None})
    job["updated_at"] = _now_iso()
    write_json_atomic(job_path, job)
    return job


def latest_v4_status(*, workspace_dir: Path, url: str) -> dict[str, Any] | None:
    runs_dir = Path(workspace_dir) / "v4-storage" / "items" / item_id_for_url(url) / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        [path for path in runs_dir.iterdir() if path.is_dir()],
        key=_run_progress_mtime,
        reverse=True,
    )
    for run_dir in candidates:
        state = _read_json_optional(run_dir / "run-state.json")
        if state.get("source_url") and state["source_url"] != url:
            continue
        try:
            return inspect_run(run_dir)
        except Exception as exc:  # noqa: BLE001
            return {
                "run_dir": str(run_dir),
                "status": "unknown",
                "stage": "unknown",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return None


def summarize_v4_status(report: dict[str, Any]) -> dict[str, Any]:
    chunking = report.get("chunking", {}) or {}
    expected_chunks = _int_or_none(chunking.get("expected_chunks"))
    partials = _int_or_none(chunking.get("partials")) or 0
    audio_chunks = _int_or_none(chunking.get("audio_chunks")) or 0
    progress = None
    if expected_chunks:
        progress = min(0.95, 0.20 + (0.70 * min(partials, expected_chunks) / expected_chunks))
    elif audio_chunks:
        progress = 0.20

    stage = str(report.get("stage") or "running")
    if expected_chunks:
        message = f"{stage}: {partials}/{expected_chunks} transcription chunk(s) completed."
    elif audio_chunks:
        message = f"{stage}: {audio_chunks} audio chunk(s) prepared."
    else:
        message = f"{stage}: transcription is running."
    return {
        "stage": f"v4_{stage}",
        "message": message,
        "progress": progress,
        "v4_run_dir": report.get("run_dir"),
        "v4_status": report,
    }


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _new_job_dir(workspace_dir: Path) -> Path:
    root = Path(workspace_dir) / "mcp-jobs"
    root.mkdir(parents=True, exist_ok=True)
    while True:
        run_id = "mcpjob_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S_") + uuid.uuid4().hex[:8]
        job_dir = root / run_id
        try:
            job_dir.mkdir()
        except FileExistsError:
            continue
        return job_dir


def _refresh_job_status(job_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    if job.get("status") in TERMINAL_STATUSES:
        return job

    v4_status = latest_v4_status(
        workspace_dir=Path(str(job.get("workspace_dir") or job_dir.parents[1])),
        url=str(job.get("url") or ""),
    )
    if v4_status:
        summary = summarize_v4_status(v4_status)
        update_payload = {
            "stage": summary["stage"],
            "message": summary["message"],
            "v4_run_dir": summary["v4_run_dir"],
            "v4_status": summary["v4_status"],
        }
        if summary["progress"] is not None:
            update_payload["progress"] = summary["progress"]
        job = update_job_status(job_dir, **update_payload)

    pid = _int_or_none(job.get("worker_pid"))
    if pid and not _is_pid_alive(pid):
        if (job_dir / "result.json").exists():
            job = update_job_status(
                job_dir,
                status="completed",
                stage="completed",
                message="Transcription completed.",
                progress=1.0,
                result_available=True,
                finished_at=job.get("finished_at") or _now_iso(),
            )
        elif (job_dir / "error.json").exists():
            error = _read_json_optional(job_dir / "error.json")
            job = update_job_status(
                job_dir,
                status="failed",
                stage="failed",
                message=str(error.get("message") or "Worker failed."),
                error=error,
                finished_at=job.get("finished_at") or _now_iso(),
            )
        else:
            job = update_job_status(
                job_dir,
                status="failed",
                stage="worker_exited",
                message="Worker process exited before writing a result.",
                error="worker process exited before writing result.json or error.json",
                finished_at=job.get("finished_at") or _now_iso(),
            )
    return job


def _public_job(job_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "schema_version",
        "run_id",
        "url",
        "language",
        "status",
        "stage",
        "message",
        "progress",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "worker_pid",
        "result_available",
        "failed_attempts",
        "method",
        "v4_run_dir",
        "v4_status",
        "error",
        "logs",
    }
    public = {key: value for key, value in job.items() if key in public_keys}
    public["job_dir"] = str(job_dir)
    public["result_available"] = bool((job_dir / "result.json").exists())
    return public


def _read_job(job_dir: Path) -> dict[str, Any]:
    job_path = Path(job_dir) / "job.json"
    if not job_path.exists():
        raise JobNotFoundError(str(job_dir))
    job = read_json(job_path)
    job.setdefault("workspace_dir", str(Path(job_dir).parents[1]))
    return job


def _read_json_optional(path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _run_progress_mtime(run_dir: Path) -> float:
    candidates = [
        run_dir / "run-state.json",
        run_dir / "run.json",
        run_dir / "canonical.json",
    ]
    existing = [path.stat().st_mtime for path in candidates if path.exists()]
    return max(existing) if existing else run_dir.stat().st_mtime


def _validate_run_id(run_id: str) -> None:
    if not run_id or any(char not in SAFE_RUN_ID_CHARS for char in run_id):
        raise ValueError("run_id contains unsupported characters")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(  # noqa: S603,S607
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_process_tree(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        completed = subprocess.run(  # noqa: S603,S607
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return completed.returncode == 0
    try:
        os.killpg(pid, 15)
    except OSError:
        return False
    return True


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
