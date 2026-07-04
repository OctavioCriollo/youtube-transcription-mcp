"""Worker process entrypoint for asynchronous MCP transcription jobs."""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from transcription_mcp.jobs import (
    latest_engine_status,
    read_json,
    summarize_engine_status,
    update_job_status,
    write_json_atomic,
)
from transcription_mcp.pipeline import (
    transcribe_file_sync,
    transcribe_media_url_sync,
    transcribe_youtube_sync,
)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 1:
        print("usage: python -m transcription_mcp.worker <job_dir>", file=sys.stderr)
        return 2

    job_dir = Path(argv[0]).resolve()
    request = read_json(job_dir / "request.json")
    source = str(request["source"])
    source_type = str(request.get("source_type") or "youtube")
    language = request.get("language")
    workspace_dir = Path(str(request["workspace_dir"]))

    stop_monitor = threading.Event()
    monitor = threading.Thread(
        target=_monitor_engine_status,
        kwargs={
            "job_dir": job_dir,
            "workspace_dir": workspace_dir,
            "source": source,
            "source_type": source_type,
            "stop_event": stop_monitor,
        },
        daemon=True,
    )
    monitor.start()

    try:
        update_job_status(
            job_dir,
            status="running",
            stage="started",
            message="Transcription worker started.",
            progress=0.05,
            started_at=_now_marker(),
            heartbeat_at=_now_marker(),
        )
        result = _run_request(
            request=request,
            source=source,
            source_type=source_type,
            language=language,
            workspace_dir=workspace_dir,
            status_callback=lambda event: _on_pipeline_status(job_dir, event),
        )
        write_json_atomic(job_dir / "result.json", result)
        update_job_status(
            job_dir,
            status="completed",
            stage="completed",
            message=f"Transcription completed with {result.get('method', 'unknown')}.",
            progress=1.0,
            method=result.get("method"),
            result_available=True,
            finished_at=_now_marker(),
        )
        return 0
    except BaseException as exc:  # noqa: BLE001
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json_atomic(job_dir / "error.json", error)
        update_job_status(
            job_dir,
            status="failed",
            stage="failed",
            message=f"{type(exc).__name__}: {exc}",
            error=error,
            finished_at=_now_marker(),
        )
        return 1
    finally:
        stop_monitor.set()
        monitor.join(timeout=2)


def _run_request(
    *,
    request: dict[str, Any],
    source: str,
    source_type: str,
    language: str | None,
    workspace_dir: Path,
    status_callback,
) -> dict[str, Any]:
    common = {
        "language": language,
        "workspace_dir": workspace_dir,
        "provider_order": request.get("provider_order"),
        "diarize": bool(request.get("diarize", False)),
        "num_speakers": request.get("num_speakers"),
        "cache_ttl_hours": request.get("cache_ttl_hours"),
        "status_callback": status_callback,
    }
    if source_type == "file":
        return transcribe_file_sync(file_path=Path(source), **common)

    url_common = {
        **common,
        "url": source,
        "ytdlp_cookies_file": (
            Path(str(request["ytdlp_cookies_file"]))
            if request.get("ytdlp_cookies_file")
            else None
        ),
        "ytdlp_proxy": request.get("ytdlp_proxy"),
        "managed_cookies_file": (
            Path(str(request["managed_cookies_file"]))
            if request.get("managed_cookies_file")
            else None
        ),
        "managed_cookies_idle_ttl_s": float(
            request.get("managed_cookies_idle_ttl_s") or 86_400.0
        ),
    }
    if source_type == "media_url":
        return transcribe_media_url_sync(**url_common)
    return transcribe_youtube_sync(**url_common)


def _on_pipeline_status(job_dir: Path, event: dict[str, Any]) -> None:
    stage = str(event.get("stage") or "running")
    payload: dict[str, Any] = {
        "status": "failed" if stage == "failed" else "running",
        "stage": stage,
        "message": str(event.get("message") or stage),
    }
    for key in ("method", "failed_attempts", "run_dir"):
        if key in event:
            payload[key] = event[key]
    if stage == "completed":
        payload["progress"] = 0.98
    elif stage.endswith("_started"):
        payload["progress"] = max(float(read_json(job_dir / "job.json").get("progress") or 0), 0.10)
    update_job_status(job_dir, **payload)


def _monitor_engine_status(
    *,
    job_dir: Path,
    workspace_dir: Path,
    source: str,
    source_type: str,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(2.0):
        job = read_json(job_dir / "job.json")
        if job.get("status") in {"completed", "failed", "canceled", "stale_failed"}:
            return
        # Heartbeat: the worker is alive and processing. Status readers use this
        # to distinguish a live long job from a hung/dead one.
        report = latest_engine_status(
            workspace_dir=workspace_dir,
            source=source,
            source_type=source_type,
        )
        update_payload: dict[str, Any] = {"heartbeat_at": _now_marker()}
        if report:
            summary = summarize_engine_status(report)
            update_payload.update(
                {
                    "status": "running",
                    "stage": summary["stage"],
                    "message": summary["message"],
                    "engine_run_dir": summary["engine_run_dir"],
                    "engine_status": summary["engine_status"],
                }
            )
            if summary["progress"] is not None:
                update_payload["progress"] = summary["progress"]
        update_job_status(job_dir, **update_payload)


def _now_marker() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    raise SystemExit(main())
