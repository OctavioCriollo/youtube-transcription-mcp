"""Persistent MCP job control for long-running transcriptions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcription_v4.status import inspect_run
from transcription_v4.storage import item_id_for_url

from transcription_mcp.config import STORAGE_DIR_NAME

JOB_SCHEMA_VERSION = "mcp-transcription-job-v1"
# stale_failed is terminal: a job that hung (no heartbeat) is moved here so it
# stops counting against max_concurrent_jobs.
TERMINAL_STATUSES = {"completed", "failed", "canceled", "stale_failed"}
SOURCE_TYPES = {"youtube", "media_url", "file"}
SAFE_RUN_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
DEFAULT_RECOMMENDED_POLL_SECONDS = 20


def _stale_seconds_from_env() -> float:
    raw = os.environ.get("TRANSCRIPTION_JOB_STALE_SECONDS", "180").strip()
    try:
        value = float(raw)
    except ValueError:
        return 180.0
    return value if value > 0 else 0.0


# Seconds without a heartbeat before a running job is considered hung.
# 0 disables stale detection.
JOB_STALE_SECONDS = _stale_seconds_from_env()


class JobNotFoundError(FileNotFoundError):
    """Requested job id does not exist in the MCP job store."""


def start_transcription_job(
    *,
    source: str | None = None,
    source_type: str = "youtube",
    url: str | None = None,
    language: str | None,
    workspace_dir: Path,
    provider_order: str | None = None,
    diarize: bool = False,
    num_speakers: int | None = None,
    ytdlp_cookies_file: Path | None = None,
    ytdlp_proxy: str | None = None,
    cache_ttl_hours: float | None = 24.0,
    max_concurrent_jobs: int = 2,
    job_ttl_hours: float | None = 168.0,
) -> dict[str, Any]:
    source = str(source or url or "").strip()
    if not source:
        raise ValueError("source must not be empty")
    source_type = source_type.strip().lower()
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of: {', '.join(sorted(SOURCE_TYPES))}")
    if source_type == "file" and not Path(source).expanduser().is_file():
        raise FileNotFoundError(Path(source).expanduser())

    cleanup_expired_jobs(workspace_dir=workspace_dir, ttl_hours=job_ttl_hours)
    active_jobs = count_active_jobs(workspace_dir=workspace_dir)
    if active_jobs >= max_concurrent_jobs:
        raise RuntimeError(
            f"maximum concurrent transcription jobs reached "
            f"({active_jobs}/{max_concurrent_jobs})"
        )

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
        "source": source,
        "source_type": source_type,
        "url": source if source_type in {"youtube", "media_url"} else None,
        "language": language,
        "workspace_dir": str(Path(workspace_dir)),
        "provider_order": provider_order,
        "diarize": diarize,
        "num_speakers": num_speakers,
        "ytdlp_cookies_file": str(ytdlp_cookies_file) if ytdlp_cookies_file else None,
        "ytdlp_proxy": ytdlp_proxy,
        "cache_ttl_hours": cache_ttl_hours,
    }
    job = {
        "schema_version": JOB_SCHEMA_VERSION,
        "run_id": run_id,
        "source": source,
        "source_type": source_type,
        "url": source if source_type in {"youtube", "media_url"} else None,
        "language": language,
        "provider_order": provider_order,
        "diarize": diarize,
        "num_speakers": num_speakers,
        "status": "queued",
        "stage": "queued",
        "message": "Transcription job queued.",
        "progress": 0.0,
        "revision": 0,
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


WATCH_MAX_TIMEOUT_SECONDS = 30.0
WATCH_POLL_INTERVAL_SECONDS = 0.7


async def watch_transcription_job(
    *,
    run_id: str,
    workspace_dir: Path,
    since_revision: int | None = None,
    timeout_seconds: float = 25.0,
) -> dict[str, Any]:
    """Long-poll a job: block until its `revision` changes (a new stage/status) or
    it is terminal, or until timeout, then return the same contract as
    get_transcription_job_status plus `changed` and `terminal`.

    Async on purpose: it awaits between polls so it does not tie up a server thread
    while waiting. The agent calls it in a loop (passing the last `revision` as
    `since_revision`) to follow progress without yielding its turn.
    """
    import anyio

    try:
        requested = float(timeout_seconds)
    except (TypeError, ValueError):
        requested = 25.0
    timeout = max(1.0, min(requested, WATCH_MAX_TIMEOUT_SECONDS))
    baseline = int(since_revision) if since_revision is not None else None
    deadline = time.monotonic() + timeout

    while True:
        status = get_transcription_job_status(run_id=run_id, workspace_dir=workspace_dir)
        revision = int(status.get("revision") or 0)
        terminal = str(status.get("status")) in TERMINAL_STATUSES
        changed = baseline is None or revision != baseline
        if changed or terminal or time.monotonic() >= deadline:
            result = dict(status)
            result["changed"] = bool(changed or terminal)
            result["terminal"] = terminal
            if not terminal:
                # Keep the agent in the watch loop instead of yielding.
                result["recommended_next_tool"] = "watch_transcription"
            return result
        await anyio.sleep(WATCH_POLL_INTERVAL_SECONDS)


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
            "progress": status.get("progress"),
            "logs": status.get("logs"),
            "result_available": False,
        }
        if status.get("failed_attempts") is not None:
            response["failed_attempts"] = status.get("failed_attempts")
        if status.get("method") is not None:
            response["method"] = status.get("method")
        if status["status"] == "failed":
            response["error"] = _read_json_optional(job_dir / "error.json")
        return _with_agent_guidance(response, response_type="result")

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

    return _with_agent_guidance(
        {
            "run_id": run_id,
            "status": "completed",
            "result_available": True,
            "result": read_json(result_path),
        },
        response_type="result",
    )


def create_transcription_job_bundle(
    *,
    run_id: str,
    workspace_dir: Path,
    openclaw_workspace_dir: str | None = None,
    ttl_hours: float | None = 24.0,
) -> dict[str, Any]:
    """Create a delivery .zip for a completed job and return its metadata.

    Resolves the official run_dir from the job's result.json (source of truth),
    then packages the run's artifacts. The agent should send
    bundle_path_for_openclaw to the user — never reconstruct files by hand.
    """
    from transcription_mcp.bundle import BundleError, create_bundle

    result_response = get_transcription_job_result(run_id=run_id, workspace_dir=workspace_dir)
    if result_response.get("status") != "completed":
        # Not ready (queued/running/failed/...). Pass the status response through
        # so the agent follows recommended_next_tool.
        return result_response

    result = result_response["result"]
    run_dir = result.get("run_dir")
    if not run_dir:
        return {
            "run_id": run_id,
            "status": "error",
            "error": "completed job has no run_dir; cannot build a bundle",
        }

    try:
        meta = create_bundle(
            run_dir=Path(str(run_dir)),
            workspace_dir=workspace_dir,
            openclaw_workspace_dir=openclaw_workspace_dir,
            ttl_hours=ttl_hours,
        )
    except BundleError as exc:
        return {"run_id": run_id, "status": "error", "error": str(exc)}

    meta["run_id"] = run_id
    return _with_agent_guidance(meta, response_type="bundle")


def get_transcription_job_artifact(
    *,
    run_id: str,
    artifact: str,
    workspace_dir: Path,
    max_chars: int = 200_000,
) -> dict[str, Any]:
    result_response = get_transcription_job_result(run_id=run_id, workspace_dir=workspace_dir)
    if result_response.get("status") != "completed":
        return result_response

    result = result_response["result"]
    artifacts = result.get("artifacts", {}) or {}
    if artifact not in artifacts:
        return _with_agent_guidance(
            {
                "run_id": run_id,
                "status": "not_found",
                "artifact": artifact,
                "available_artifacts": sorted(artifacts),
            },
            response_type="artifact",
        )

    run_dir = Path(str(result["run_dir"])).resolve()
    artifact_path = Path(str(artifacts[artifact]["path"])).resolve()
    if not _is_relative_to(artifact_path, run_dir):
        raise ValueError("artifact path escapes run_dir")
    content = artifact_path.read_text(encoding="utf-8", errors="replace")
    truncated = len(content) > max_chars
    return _with_agent_guidance(
        {
            "run_id": run_id,
            "status": "completed",
            "artifact": artifact,
            "path": str(artifact_path),
            "size_bytes": artifact_path.stat().st_size,
            "truncated": truncated,
            "content": content[:max_chars],
        },
        response_type="artifact",
    )


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


def cleanup_expired_jobs(*, workspace_dir: Path, ttl_hours: float | None) -> int:
    if ttl_hours is None:
        return 0
    jobs_root = Path(workspace_dir) / "mcp-jobs"
    if not jobs_root.is_dir():
        return 0
    cutoff = datetime.now(UTC).timestamp() - (ttl_hours * 3600)
    removed = 0
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir():
            continue
        job = _read_json_optional(job_dir / "job.json")
        if job.get("status") not in TERMINAL_STATUSES:
            continue
        marker = job.get("finished_at") or job.get("updated_at")
        marker_ts = _parse_iso_timestamp(marker) if marker else None
        if marker_ts is None:
            marker_ts = (job_dir / "job.json").stat().st_mtime if (job_dir / "job.json").exists() else job_dir.stat().st_mtime
        if marker_ts >= cutoff:
            continue
        shutil.rmtree(job_dir, ignore_errors=True)
        removed += 1
    return removed


def count_active_jobs(*, workspace_dir: Path) -> int:
    jobs_root = Path(workspace_dir) / "mcp-jobs"
    if not jobs_root.is_dir():
        return 0
    count = 0
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir():
            continue
        job = _read_json_optional(job_dir / "job.json")
        if not job or job.get("status") in TERMINAL_STATUSES:
            continue
        # A hung job (no recent heartbeat) does not count against concurrency,
        # even if its status on disk still says running.
        if _is_job_stale(job):
            continue
        pid = _int_or_none(job.get("worker_pid"))
        if pid is None or _is_pid_alive(pid):
            count += 1
    return count


def get_job_dir(*, workspace_dir: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    job_dir = Path(workspace_dir) / "mcp-jobs" / run_id
    if not job_dir.is_dir():
        raise JobNotFoundError(run_id)
    return job_dir


def update_job_status(job_dir: Path, **updates: Any) -> dict[str, Any]:
    job_path = Path(job_dir) / "job.json"
    job = read_json(job_path) if job_path.exists() else {}
    prev_status = job.get("status")
    prev_stage = job.get("stage")
    job.update({key: value for key, value in updates.items() if value is not None})
    # Bump `revision` only on a milestone change (status or stage), NOT on every
    # heartbeat write. watch_transcription wakes on revision changes, so this keeps
    # it firing on real progress (stage transitions, terminal states) instead of
    # every 2s heartbeat. progress/message still ride along in the snapshot.
    if job.get("status") != prev_status or job.get("stage") != prev_stage:
        job["revision"] = int(job.get("revision") or 0) + 1
    job["updated_at"] = _now_iso()
    write_json_atomic(job_path, job)
    return job


def latest_v4_status(
    *,
    workspace_dir: Path,
    source: str,
    source_type: str = "youtube",
) -> dict[str, Any] | None:
    if source_type == "file":
        # Avoid hashing large files every status poll. The final result still
        # contains the v4 run_dir and artifact manifest once the worker ends.
        return None
    else:
        item_id = item_id_for_url(source)
    runs_dir = Path(workspace_dir) / STORAGE_DIR_NAME / "items" / item_id / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        [path for path in runs_dir.iterdir() if path.is_dir()],
        key=_run_progress_mtime,
        reverse=True,
    )
    for run_dir in candidates:
        state = _read_json_optional(run_dir / "run-state.json")
        if source_type != "file" and state.get("source_url") and state["source_url"] != source:
            continue
        if source_type == "file" and state.get("source_path") and state["source_path"] != str(Path(source).expanduser().resolve()):
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

    report = latest_v4_status(
        workspace_dir=Path(str(job.get("workspace_dir") or job_dir.parents[1])),
        source=str(job.get("source") or job.get("url") or ""),
        source_type=str(job.get("source_type") or "youtube"),
    )
    if report:
        summary = summarize_v4_status(report)
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
    pid_dead = bool(pid) and not _is_pid_alive(pid)

    if pid_dead:
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

    # PID still alive (or unknown) but the worker has not emitted a heartbeat
    # within JOB_STALE_SECONDS -> treat as hung. This prevents a stuck job from
    # holding a max_concurrent_jobs slot forever. The marker is terminal, so it
    # stops counting against concurrency and the user gets a clear state.
    if _is_job_stale(job):
        job = update_job_status(
            job_dir,
            status="stale_failed",
            stage="stale",
            message=(
                f"Job exceeded {JOB_STALE_SECONDS}s without a heartbeat and was "
                "marked stale. Cancel/retry; it no longer blocks concurrency."
            ),
            error="no heartbeat within JOB_STALE_SECONDS (worker hung or unresponsive)",
            finished_at=job.get("finished_at") or _now_iso(),
        )
    return job


def _is_job_stale(job: dict[str, Any]) -> bool:
    """True if a non-terminal job has not produced a heartbeat recently."""
    if job.get("status") in TERMINAL_STATUSES:
        return False
    if JOB_STALE_SECONDS <= 0:
        return False
    marker = job.get("heartbeat_at") or job.get("started_at") or job.get("updated_at")
    marker_ts = _parse_iso_timestamp(marker) if marker else None
    if marker_ts is None:
        return False
    return (datetime.now(UTC).timestamp() - marker_ts) > JOB_STALE_SECONDS


def _public_job(job_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "schema_version",
        "run_id",
        "source",
        "source_type",
        "url",
        "language",
        "diarize",
        "num_speakers",
        "status",
        "stage",
        "message",
        "progress",
        "revision",
        "created_at",
        "updated_at",
        "started_at",
        "heartbeat_at",
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
    return _with_agent_guidance(public, response_type="status")


def _with_agent_guidance(payload: dict[str, Any], *, response_type: str) -> dict[str, Any]:
    guided = dict(payload)
    progress_percent = _progress_percent(guided)
    if progress_percent is not None:
        guided["progress_percent"] = progress_percent
    guided.update(_agent_guidance(guided, response_type=response_type))
    return guided


def _agent_guidance(payload: dict[str, Any], *, response_type: str) -> dict[str, Any]:
    status = str(payload.get("status") or "unknown")
    run_id = str(payload.get("run_id") or "")
    progress_percent = payload.get("progress_percent")
    artifacts = _artifact_names_from_payload(payload)

    if status in {"queued", "running", "canceling"}:
        return {
            "user_visible_message": _running_user_message(payload),
            "recommended_next_tool": "get_transcription_status",
            "recommended_poll_seconds": DEFAULT_RECOMMENDED_POLL_SECONDS,
            "agent_instructions": [
                "Tell the user the transcription is still processing.",
                "Keep the run_id and call get_transcription_status again after "
                "recommended_poll_seconds.",
                "When status becomes completed, call get_transcription_result before "
                "answering with the transcript.",
            ],
        }

    if status == "completed" and response_type == "bundle":
        return {
            "user_visible_message": "Transcription bundle (.zip) is ready to send.",
            "recommended_next_tool": None,
            "recommended_poll_seconds": None,
            "agent_instructions": [
                "Send the file at bundle_path_for_openclaw to the user as an attachment.",
                "Do NOT rebuild the file by hand or send a plain .txt; use this .zip.",
                "If the bundle expired or is missing later, call this tool again to regenerate it.",
            ],
        }

    if status == "completed" and response_type == "status":
        return {
            "user_visible_message": (
                f"Transcription job {run_id} is complete. Fetch the final result."
            ),
            "recommended_next_tool": "get_transcription_result",
            "recommended_poll_seconds": None,
            "agent_instructions": [
                "Tell the user the transcription has completed.",
                "Call get_transcription_result with this run_id before giving the transcript.",
            ],
        }

    if status == "completed" and response_type == "result":
        message = "Transcription result is ready."
        if artifacts:
            message += " Optional artifacts are available for timestamps, subtitles, or audit data."
        return {
            "user_visible_message": message,
            "recommended_next_tool": None,
            "recommended_poll_seconds": None,
            "recommended_artifacts": artifacts,
            "available_next_actions": _result_next_actions(artifacts),
            "agent_instructions": [
                "Use result.transcript as the main answer unless the user requested a format file.",
                "Do not poll status again; this job is already terminal.",
                "Use get_transcription_artifact only when the user asks for timestamps, "
                "subtitles, audit data, or another listed artifact.",
            ],
        }

    if status == "completed" and response_type == "artifact":
        artifact = str(payload.get("artifact") or "artifact")
        return {
            "user_visible_message": f"Artifact {artifact} is ready and included in content.",
            "recommended_next_tool": None,
            "recommended_poll_seconds": None,
            "agent_instructions": [
                "Use content as the artifact body.",
                "If truncated is true, explain that only the first max_chars were returned.",
            ],
        }

    if status == "not_found":
        artifact = str(payload.get("artifact") or "artifact")
        return {
            "user_visible_message": (
                f"Artifact {artifact} was not found. Available artifacts: "
                f"{', '.join(artifacts) if artifacts else 'none'}."
            ),
            "recommended_next_tool": "get_transcription_artifact" if artifacts else None,
            "recommended_poll_seconds": None,
            "recommended_artifacts": artifacts,
            "agent_instructions": [
                "Do not retry the same artifact name.",
                "If the user wants artifact content, call get_transcription_artifact with one "
                "of the available_artifacts values.",
            ],
        }

    if status == "failed":
        return {
            "user_visible_message": _terminal_user_message(payload, fallback="Transcription failed."),
            "recommended_next_tool": None,
            "recommended_poll_seconds": None,
            "agent_instructions": [
                "Tell the user the transcription failed.",
                "Use error, failed_attempts, and logs if present to explain the likely cause.",
                "Do not keep polling this run_id unless the job state changes externally.",
            ],
        }

    if status == "stale_failed":
        return {
            "user_visible_message": _terminal_user_message(
                payload,
                fallback="Transcription stalled (no progress) and was stopped.",
            ),
            "recommended_next_tool": "start_youtube_transcription",
            "recommended_poll_seconds": None,
            "agent_instructions": [
                "Tell the user the transcription stalled and was stopped automatically.",
                "This run_id is terminal; do not poll it. Start a new transcription to retry.",
                "It no longer blocks concurrency.",
            ],
        }

    if status == "canceled":
        return {
            "user_visible_message": _terminal_user_message(
                payload,
                fallback="Transcription was canceled.",
            ),
            "recommended_next_tool": None,
            "recommended_poll_seconds": None,
            "agent_instructions": [
                "Tell the user the transcription was canceled.",
                "Do not call get_transcription_result for this run_id.",
            ],
        }

    return {
        "user_visible_message": _unknown_user_message(payload, progress_percent),
        "recommended_next_tool": "get_transcription_status",
        "recommended_poll_seconds": DEFAULT_RECOMMENDED_POLL_SECONDS,
        "agent_instructions": [
            "Report user_visible_message to the user.",
            "Poll get_transcription_status again unless the status becomes terminal.",
        ],
    }


def _running_user_message(payload: dict[str, Any]) -> str:
    run_id = str(payload.get("run_id") or "")
    status = str(payload.get("status") or "running")
    stage = str(payload.get("stage") or status)
    message = str(payload.get("message") or "Transcription is processing.")
    progress_percent = payload.get("progress_percent")

    parts = [f"Transcription job {run_id} is {status} ({stage}).", message]
    if progress_percent is not None:
        parts.append(f"Approximate progress: {progress_percent}%.")
    return " ".join(parts)


def _terminal_user_message(payload: dict[str, Any], *, fallback: str) -> str:
    message = str(payload.get("message") or "").strip()
    return message or fallback


def _unknown_user_message(payload: dict[str, Any], progress_percent: Any) -> str:
    status = str(payload.get("status") or "unknown")
    stage = str(payload.get("stage") or status)
    message = str(payload.get("message") or "Transcription state is being refreshed.")
    parts = [f"Transcription status is {status} ({stage}).", message]
    if progress_percent is not None:
        parts.append(f"Approximate progress: {progress_percent}%.")
    return " ".join(parts)


def _result_next_actions(artifacts: list[str]) -> list[str]:
    actions = ["respond_with_transcript", "summarize_transcript"]
    artifact_actions = {
        "transcript_timestamps_txt": "fetch_timestamped_transcript",
        "subtitles_srt": "fetch_srt_subtitles",
        "subtitles_vtt": "fetch_vtt_subtitles",
        "audit_txt": "inspect_quality_audit",
    }
    for artifact in artifacts:
        action = artifact_actions.get(artifact)
        if action and action not in actions:
            actions.append(action)
    return actions


def _artifact_names_from_payload(payload: dict[str, Any]) -> list[str]:
    available_artifacts = payload.get("available_artifacts")
    if isinstance(available_artifacts, list):
        return sorted(str(artifact) for artifact in available_artifacts)

    result = payload.get("result")
    if isinstance(result, dict):
        artifacts = result.get("artifacts") or {}
        if isinstance(artifacts, dict):
            return sorted(str(artifact) for artifact in artifacts)
    return []


def _progress_percent(payload: dict[str, Any]) -> int | None:
    status = str(payload.get("status") or "")
    if status == "completed":
        return 100

    progress = payload.get("progress")
    try:
        progress_float = float(progress)
    except (TypeError, ValueError):
        return None

    return max(0, min(100, round(progress_float * 100)))


def _read_job(job_dir: Path) -> dict[str, Any]:
    job_path = Path(job_dir) / "job.json"
    if not job_path.exists():
        raise JobNotFoundError(str(job_dir))
    job = read_json(job_path)
    job.setdefault("workspace_dir", str(Path(job_dir).parents[1]))
    job.setdefault("source", job.get("url"))
    job.setdefault("source_type", "youtube")
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


def _parse_iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
