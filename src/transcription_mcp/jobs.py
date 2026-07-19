"""Persistent MCP job control for long-running transcriptions."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcription_engine.status import inspect_run
from transcription_engine.storage import item_id_for_url

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
    managed_cookies_file: Path | None = None,
    managed_cookies_idle_ttl_s: float = 86_400.0,
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

    # Dedup guard: an ACTIVE job for the same source and options is reused
    # instead of spawning a second pipeline. Impatient agents otherwise start a
    # duplicate "just in case" and pay the provider twice for the same audio.
    duplicate = _find_active_duplicate(
        workspace_dir=workspace_dir,
        source=source,
        source_type=source_type,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
    )
    if duplicate is not None:
        dup_dir, dup_job = duplicate
        public = _public_job(dup_dir, dup_job)
        public["deduplicated"] = True
        public["user_visible_message"] = (
            f"A transcription job for this exact source is already active "
            f"(run_id {dup_job.get('run_id')}); reusing it instead of starting "
            f"a duplicate. " + str(public.get("user_visible_message") or "")
        ).strip()
        public["agent_instructions"] = [
            "This source is ALREADY being transcribed; no new job was started.",
            "Do not retry with another transcribe tool - that would duplicate provider cost.",
            "Follow this run_id with watch_transcription until it completes, then call "
            "get_transcription_result.",
        ]
        return public

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
        "managed_cookies_file": str(managed_cookies_file) if managed_cookies_file else None,
        "managed_cookies_idle_ttl_s": managed_cookies_idle_ttl_s,
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
            if not result["changed"]:
                # An unchanged long-poll is the moment agents panic and start
                # duplicate jobs. Say explicitly that quiet != stuck.
                heartbeat_age = result.get("heartbeat_age_seconds")
                if isinstance(heartbeat_age, (int, float)) and heartbeat_age <= 30:
                    liveness = (
                        f"the worker is alive (heartbeat {int(heartbeat_age)}s ago)"
                    )
                else:
                    liveness = (
                        "if the worker heartbeat stays silent the job will be marked "
                        "stale_failed automatically - no manual restart is needed"
                    )
                result["note"] = (
                    f"No new milestone within {timeout:.0f}s, but {liveness}. This is "
                    "normal while a provider processes a long file. Call "
                    "watch_transcription again with this revision; do NOT start a "
                    "duplicate transcription."
                )
            return result
        await anyio.sleep(WATCH_POLL_INTERVAL_SECONDS)


def get_transcription_job_result(
    *,
    run_id: str,
    workspace_dir: Path,
) -> dict[str, Any]:
    status = get_transcription_job_status(run_id=run_id, workspace_dir=workspace_dir)
    job_dir = get_job_dir(workspace_dir=workspace_dir, run_id=run_id)
    result_path = job_dir / "result.json"

    # Disk is authoritative: if a complete result.json exists, serve it no matter
    # what the status flag says. A worker can finish (result.json written) and
    # then die/hang before flipping the flag; the transcript is still valid and
    # must be delivered, not withheld behind a status that never advanced.
    if _has_complete_result(job_dir):
        return _with_agent_guidance(
            {
                "run_id": run_id,
                "status": "completed",
                "result_available": True,
                "result": read_json(result_path),
            },
            response_type="result",
        )

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

    # status says completed but result.json is missing/unreadable -> mark failed.
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


def _find_active_duplicate(
    *,
    workspace_dir: Path,
    source: str,
    source_type: str,
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest ACTIVE job that would transcribe the same thing.

    Identity matches the engine's own storage identity: URL sources compare by
    item_id_for_url (same hash the cache uses), file sources by resolved path.
    Options must match too - a diarized request is NOT a duplicate of a plain
    one. Terminal, stale, and dead-worker jobs never count.
    """
    jobs_root = Path(workspace_dir) / "mcp-jobs"
    if not jobs_root.is_dir():
        return None
    if source_type == "file":
        source_key = str(Path(source).expanduser().resolve())
    else:
        source_key = item_id_for_url(source)

    # run_ids embed a UTC timestamp, so reverse name order == newest first.
    for job_dir in sorted(
        (path for path in jobs_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        job = _read_json_optional(job_dir / "job.json")
        if not job or job.get("status") in TERMINAL_STATUSES:
            continue
        if _is_job_stale(job):
            continue
        pid = _int_or_none(job.get("worker_pid"))
        if pid is not None and not _is_pid_alive(pid):
            continue
        if str(job.get("source_type") or "youtube") != source_type:
            continue
        candidate_source = str(job.get("source") or job.get("url") or "")
        if not candidate_source:
            continue
        if source_type == "file":
            candidate_key = str(Path(candidate_source).expanduser().resolve())
        else:
            candidate_key = item_id_for_url(candidate_source)
        if candidate_key != source_key:
            continue
        if job.get("language") != language:
            continue
        if bool(job.get("diarize", False)) != bool(diarize):
            continue
        if job.get("num_speakers") != num_speakers:
            continue
        return job_dir, job
    return None


async def run_transcription_job_with_budget(
    *,
    budget_seconds: float,
    workspace_dir: Path,
    **start_kwargs: Any,
) -> dict[str, Any]:
    """Run a transcription as a background job, waiting up to budget_seconds.

    This is the engine behind the synchronous transcribe_* tools. The old
    behavior blocked the tool call for the whole pipeline; on long sources the
    MCP client timed out first, the agent read that as a failure, and the
    orphaned server-side run kept billing the provider. Instead: start (or
    dedup onto) a job, watch it up to the budget, and either return the final
    result or hand the still-running job off to watch_transcription.
    """
    status = start_transcription_job(workspace_dir=workspace_dir, **start_kwargs)
    run_id = str(status["run_id"])
    deadline = time.monotonic() + max(5.0, float(budget_seconds))
    revision = _int_or_none(status.get("revision"))
    terminal = str(status.get("status")) in TERMINAL_STATUSES

    while not terminal:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        status = await watch_transcription_job(
            run_id=run_id,
            workspace_dir=workspace_dir,
            since_revision=revision,
            timeout_seconds=min(remaining, WATCH_MAX_TIMEOUT_SECONDS),
        )
        revision = _int_or_none(status.get("revision"))
        terminal = bool(status.get("terminal"))

    if terminal:
        # Completed AND failed/canceled both flow through the result contract,
        # which already carries agent guidance for each terminal state.
        return get_transcription_job_result(run_id=run_id, workspace_dir=workspace_dir)

    handoff = dict(status)
    handoff["sync_budget_exceeded"] = True
    handoff["recommended_next_tool"] = "watch_transcription"
    handoff["user_visible_message"] = (
        f"The transcription needs more than {budget_seconds:.0f}s and continues in "
        f"the background as job {run_id}. "
        + str(handoff.get("user_visible_message") or "")
    ).strip()
    handoff["agent_instructions"] = [
        "The job is STILL RUNNING in the background; this is a handoff, not a failure.",
        "Do NOT call a transcribe tool again for this source - that would duplicate "
        "provider cost.",
        "Follow this run_id with watch_transcription (pass the returned revision as "
        "since_revision) until terminal, then call get_transcription_result.",
    ]
    return handoff


def get_job_dir(*, workspace_dir: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    job_dir = Path(workspace_dir) / "mcp-jobs" / run_id
    if not job_dir.is_dir():
        raise JobNotFoundError(run_id)
    return job_dir


# Per-path in-process locks, so the worker's main and heartbeat threads never
# interleave a read-modify-write of the same job.json.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _job_write_lock(job_path: Path) -> Iterator[None]:
    """Serialize the read-modify-write of a job.json.

    Two layers: a process-local threading.Lock (covers the worker's two threads)
    and a POSIX fcntl lock on a sidecar file (covers worker vs MCP-server, which
    are separate processes). The fcntl layer is best-effort: on platforms without
    it (Windows dev/tests) the thread lock still holds, and correctness of the
    critical outcome no longer depends on this lock anyway (result.json on disk
    is authoritative — see _refresh_job_status).
    """
    thread_lock = _thread_lock_for(job_path)
    thread_lock.acquire()
    fd = None
    try:
        try:
            import fcntl

            lock_path = job_path.with_name(job_path.name + ".lock")
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            fd = None  # best effort; the thread lock still serializes in-process
        yield
    finally:
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        thread_lock.release()


def update_job_status(job_dir: Path, **updates: Any) -> dict[str, Any]:
    job_path = Path(job_dir) / "job.json"
    # The read, the mutate, and the write must be one atomic unit, or a
    # concurrent writer (heartbeat thread, server refresh) working from a stale
    # read can clobber a milestone — e.g. flip "completed" back to "running".
    with _job_write_lock(job_path):
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


def latest_engine_status(
    *,
    workspace_dir: Path,
    source: str,
    source_type: str = "youtube",
) -> dict[str, Any] | None:
    if source_type == "file":
        # Avoid hashing large files every status poll. The final result still
        # contains the engine run_dir and artifact manifest once the worker ends.
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


def summarize_engine_status(report: dict[str, Any]) -> dict[str, Any]:
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
    provider = str((report.get("model") or {}).get("provider") or "")
    if expected_chunks:
        message = f"{stage}: {partials}/{expected_chunks} transcription chunk(s) completed."
    elif audio_chunks:
        message = f"{stage}: {audio_chunks} audio chunk(s) prepared."
    elif provider == "elevenlabs":
        # The ElevenLabs source_url path is ONE opaque remote call: ElevenLabs
        # fetches the source on ITS OWN infrastructure and transcribes it there.
        # Nothing is downloaded on this server and no intermediate artifacts
        # appear, so the stage sits still for minutes on long videos. Say so
        # explicitly - agents otherwise invent a stuck-download story.
        message = (
            f"{stage}: ElevenLabs is fetching and transcribing the source remotely "
            "on its own servers (nothing is downloaded on this server). This is a "
            "single opaque call with no intermediate progress; long videos can sit "
            "in this state for several minutes while the worker stays healthy."
        )
    else:
        message = f"{stage}: transcription is running."
    return {
        "stage": f"engine_{stage}",
        "message": message,
        "progress": progress,
        "engine_run_dir": report.get("run_dir"),
        "engine_status": report,
    }


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per writer: job.json is written by up to three writers at once
    # (worker main thread, worker heartbeat thread, and the MCP server process
    # via _refresh_job_status). A shared "<name>.tmp" let concurrent writes
    # collide and clobber each other mid-rename. A per-write name makes each
    # atomic replace independent.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        # If replace() failed, don't leave the tmp behind.
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _has_complete_result(job_dir: Path) -> bool:
    """True if result.json exists and parses.

    write_json_atomic renames into place, so a present result.json is complete;
    the parse check is belt-and-suspenders against a truncated/foreign file.
    """
    path = Path(job_dir) / "result.json"
    if not path.exists():
        return False
    try:
        return bool(read_json(path))
    except (OSError, json.JSONDecodeError):
        return False


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

    # Disk is authoritative. A complete result.json means the transcription
    # finished — no matter whether the worker then died, hung, or had its PID
    # reused before flipping the status. Check it FIRST, ahead of any pid or
    # heartbeat logic, so a finished job is never lost to a false "stale" verdict
    # (the exact failure seen on 2026-07-08: result.json complete on disk, yet
    # the job was marked stale_failed 180s later).
    if _has_complete_result(job_dir):
        return update_job_status(
            job_dir,
            status="completed",
            stage="completed",
            message="Transcription completed.",
            progress=1.0,
            result_available=True,
            finished_at=job.get("finished_at") or _now_iso(),
        )

    report = latest_engine_status(
        workspace_dir=Path(str(job.get("workspace_dir") or job_dir.parents[1])),
        source=str(job.get("source") or job.get("url") or ""),
        source_type=str(job.get("source_type") or "youtube"),
    )
    if report:
        summary = summarize_engine_status(report)
        update_payload = {
            "stage": summary["stage"],
            "message": summary["message"],
            "engine_run_dir": summary["engine_run_dir"],
            "engine_status": summary["engine_status"],
        }
        if summary["progress"] is not None:
            update_payload["progress"] = summary["progress"]
        job = update_job_status(job_dir, **update_payload)

    pid = _int_or_none(job.get("worker_pid"))
    pid_dead = bool(pid) and not _is_pid_alive(pid)

    if pid_dead:
        # result.json was already handled above (authoritative). A dead worker
        # with no result means it failed: surface its error.json if present,
        # otherwise report that it exited before producing anything.
        if (job_dir / "error.json").exists():
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
        "engine_run_dir",
        "engine_status",
        "error",
        "logs",
    }
    public = {key: value for key, value in job.items() if key in public_keys}
    public["job_dir"] = str(job_dir)
    public["result_available"] = bool((job_dir / "result.json").exists())
    # Liveness signals for non-terminal jobs. Agents cannot see the worker
    # process, so without these a long quiet provider stage (identical
    # message, frozen progress) is indistinguishable from a hang - and
    # impatient agents respond by starting duplicate jobs.
    if public.get("status") not in TERMINAL_STATUSES:
        now_ts = datetime.now(UTC).timestamp()
        started_ts = _parse_iso_timestamp(
            str(job.get("started_at") or job.get("created_at") or "")
        )
        if started_ts is not None:
            public["elapsed_seconds"] = max(0, round(now_ts - started_ts))
        heartbeat_ts = _parse_iso_timestamp(str(job.get("heartbeat_at") or ""))
        if heartbeat_ts is not None:
            public["heartbeat_age_seconds"] = max(0, round(now_ts - heartbeat_ts))
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
            "recommended_next_tool": "watch_transcription",
            "recommended_poll_seconds": DEFAULT_RECOMMENDED_POLL_SECONDS,
            "agent_instructions": [
                "Tell the user the transcription is still processing.",
                "Call watch_transcription with this run_id and the last `revision` you "
                "saw; it waits server-side and returns on the next milestone.",
                "The job is healthy while heartbeat_age_seconds stays under ~30; an "
                "unchanged stage does NOT mean it is stuck.",
                "failed_attempts lists providers ALREADY tried and abandoned; their "
                "errors are history, not the current problem. The ACTIVE provider and "
                "what it is doing are described in `message` - do not attribute a "
                "previous provider's error (e.g. a yt-dlp download block from groq) "
                "to the provider currently running.",
                "Do NOT start another transcription for the same source while this job "
                "is active - that duplicates provider cost.",
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

    elapsed = payload.get("elapsed_seconds")
    heartbeat_age = payload.get("heartbeat_age_seconds")
    if elapsed is not None:
        parts.append(f"Elapsed: {int(elapsed)}s.")
    if heartbeat_age is not None:
        if heartbeat_age <= 30:
            parts.append(f"The worker is alive (heartbeat {int(heartbeat_age)}s ago).")
        else:
            parts.append(f"Last worker heartbeat was {int(heartbeat_age)}s ago.")
    if isinstance(elapsed, (int, float)) and elapsed >= 30:
        parts.append(
            "Long sources can spend several minutes inside one provider stage with "
            "no visible change; that is normal - keep watching instead of restarting."
        )
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
