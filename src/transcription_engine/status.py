from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from transcription_engine.chunking import plan_chunks


FINAL_ARTIFACTS = (
    "canonical.json",
    "transcript.txt",
    "transcript-timestamps.txt",
    "subtitles.srt",
    "subtitles.vtt",
    "quality.json",
    "audit.json",
    "audit.txt",
    "run.json",
)


def inspect_run(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(run_dir)

    state_path = run_dir / "run-state.json"
    run_json_path = run_dir / "run.json"
    state = _read_json_optional(state_path)
    run_record = _read_json_optional(run_json_path)
    metadata = run_record.get("metadata", {}) or {}

    partial_files = sorted((run_dir / "partials").glob("chunk_*.canonical.json"))
    chunk_files = sorted((run_dir / "media" / "chunks").glob("chunk_*.wav"))
    prepared_audio = run_dir / "media" / "prepared.wav"
    missing_final_artifacts = [
        artifact for artifact in FINAL_ARTIFACTS if not (run_dir / artifact).exists()
    ]

    duration_s = _as_float(state.get("duration_s"))
    resolved_chunk_duration_s = _as_float(state.get("resolved_chunk_duration_s"))
    overlap_s = _as_float(state.get("overlap_s")) or 0.0
    expected_chunks = _expected_chunks(
        duration_s=duration_s,
        chunk_duration_s=resolved_chunk_duration_s,
        overlap_s=overlap_s,
    )
    completed_partials = len(partial_files)
    partial_progress = (
        round(completed_partials / expected_chunks, 6)
        if expected_chunks and completed_partials <= expected_chunks
        else None
    )
    last_partial = (
        _file_summary(max(partial_files, key=lambda path: path.stat().st_mtime))
        if partial_files
        else None
    )

    status = _status(
        state=state,
        run_json_exists=run_json_path.exists(),
        missing_final_artifacts=missing_final_artifacts,
    )

    return {
        "schema_version": "4.0-status",
        "run_dir": str(run_dir),
        "status": status,
        "stage": _stage(
            status=status,
            expected_chunks=expected_chunks,
            chunk_files=len(chunk_files),
            completed_partials=completed_partials,
            prepared_audio_exists=prepared_audio.exists(),
            canonical_exists=(run_dir / "canonical.json").exists(),
        ),
        "source": {
            "path": state.get("source_path") or metadata.get("source_path"),
            "language": (
                state.get("detected_language")
                or metadata.get("detected_language")
                or metadata.get("language")
                or state.get("language")
            ),
            "requested_language": (
                state.get("language")
                if "language" in state
                else metadata.get("requested_language")
            ),
            "duration_s": duration_s,
            "duration_hms": _format_duration(duration_s),
        },
        "model": {
            "provider": state.get("provider") or metadata.get("provider") or "local",
            "profile": state.get("profile") or metadata.get("profile"),
            "model": state.get("model") or metadata.get("model"),
            "device": state.get("resolved_device")
            or metadata.get("device")
            or state.get("device"),
            "compute_type": state.get("resolved_compute_type")
            or metadata.get("compute_type")
            or state.get("compute_type"),
        },
        "chunking": {
            "mode": state.get("chunking_mode")
            or metadata.get("chunking_mode"),
            "chunk_duration_s": state.get("chunk_duration_s")
            or metadata.get("chunk_duration_s"),
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "overlap_s": overlap_s,
            "expected_chunks": expected_chunks,
            "audio_chunks": len(chunk_files),
            "partials": completed_partials,
            "partial_progress": partial_progress,
            "last_partial": last_partial,
        },
        "media": {
            "prepared_audio_exists": prepared_audio.exists(),
            "prepared_audio": _file_summary(prepared_audio) if prepared_audio.exists() else None,
        },
        "final_artifacts": {
            "complete": not missing_final_artifacts,
            "missing": missing_final_artifacts,
        },
        "timestamps": {
            "run_state_updated_at": _mtime_iso(state_path) if state_path.exists() else None,
            "run_json_updated_at": _mtime_iso(run_json_path) if run_json_path.exists() else None,
            "last_progress_at": _last_progress_at(run_dir),
        },
        "next_action": _next_action(
            status=status,
            expected_chunks=expected_chunks,
            completed_partials=completed_partials,
            final_complete=not missing_final_artifacts,
        ),
    }


def render_status_text(report: dict[str, Any]) -> str:
    source = report["source"]
    model = report["model"]
    chunking = report["chunking"]
    media = report["media"]
    final_artifacts = report["final_artifacts"]
    timestamps = report["timestamps"]

    lines = [
        "Transcription engine status",
        "",
        f"run_dir: {report['run_dir']}",
        f"status: {report['status']}",
        f"stage: {report['stage']}",
        f"source: {source['path']}",
        f"duration: {source['duration_hms'] or 'unknown'}",
        f"language: {source['language'] or 'auto'}",
        f"requested language: {source['requested_language'] or 'auto'}",
        "",
        "Model",
        f"- provider: {model['provider'] or 'unknown'}",
        f"- profile: {model['profile'] or 'unknown'}",
        f"- model: {model['model'] or 'unknown'}",
        f"- device: {model['device'] or 'unknown'}",
        f"- compute_type: {model['compute_type'] or 'unknown'}",
        "",
        "Chunking",
        f"- mode: {chunking['mode'] or 'unknown'}",
        f"- resolved chunk duration: {_format_seconds(chunking['resolved_chunk_duration_s'])}",
        f"- expected chunks: {_format_count(chunking['expected_chunks'])}",
        f"- audio chunks: {chunking['audio_chunks']}",
        f"- partials: {chunking['partials']}{_format_progress(chunking['partial_progress'])}",
        f"- last partial: {_format_file_summary(chunking['last_partial'])}",
        "",
        "Media",
        f"- prepared audio: {_format_file_summary(media['prepared_audio'])}",
        "",
        "Final artifacts",
        f"- complete: {final_artifacts['complete']}",
        f"- missing: {', '.join(final_artifacts['missing']) or 'none'}",
        "",
        "Timestamps",
        f"- run-state updated: {timestamps['run_state_updated_at'] or 'unknown'}",
        f"- run.json updated: {timestamps['run_json_updated_at'] or 'not written'}",
        f"- last progress: {timestamps['last_progress_at'] or 'unknown'}",
        "",
        f"Next action: {report['next_action']}",
    ]
    return "\n".join(lines).strip() + "\n"


def _read_json_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_chunks(
    *,
    duration_s: float | None,
    chunk_duration_s: float | None,
    overlap_s: float,
) -> int | None:
    if duration_s is None or chunk_duration_s is None:
        return None
    if duration_s <= chunk_duration_s:
        return 0
    return len(
        plan_chunks(
            duration_s=duration_s,
            chunk_duration_s=chunk_duration_s,
            overlap_s=overlap_s,
        )
    )


def _status(
    *,
    state: dict[str, Any],
    run_json_exists: bool,
    missing_final_artifacts: list[str],
) -> str:
    if run_json_exists and not missing_final_artifacts:
        return "completed"
    if run_json_exists:
        return "completed_with_missing_artifacts"
    return str(state.get("status") or "incomplete")


def _stage(
    *,
    status: str,
    expected_chunks: int | None,
    chunk_files: int,
    completed_partials: int,
    prepared_audio_exists: bool,
    canonical_exists: bool,
) -> str:
    if status.startswith("completed"):
        return "finished"
    if expected_chunks and completed_partials:
        return "transcribing_chunks"
    if expected_chunks and chunk_files:
        return "chunks_ready"
    if canonical_exists:
        return "writing_outputs"
    if prepared_audio_exists:
        return "media_prepared"
    return "initialized"


def _next_action(
    *,
    status: str,
    expected_chunks: int | None,
    completed_partials: int,
    final_complete: bool,
) -> str:
    if status == "completed" and final_complete:
        return "audit this run"
    if expected_chunks and completed_partials < expected_chunks:
        return "wait, or rerun the same command later to resume if the process stopped"
    if expected_chunks and completed_partials >= expected_chunks:
        return "wait for merge/output writing, then audit"
    return "wait, or rerun the same command later to resume if the process stopped"


def _last_progress_at(run_dir: Path) -> str | None:
    candidates = [
        path
        for pattern in (
            "run-state.json",
            "run.json",
            "canonical.json",
            "media/prepared.wav",
            "media/chunks/*.wav",
            "partials/*.canonical.json",
        )
        for path in run_dir.glob(pattern)
        if path.exists()
    ]
    if not candidates:
        return None
    return _mtime_iso(max(candidates, key=lambda path: path.stat().st_mtime))


def _file_summary(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "size": _format_bytes(stat.st_size),
        "updated_at": _mtime_iso(path),
    }


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _format_file_summary(summary: dict[str, Any] | None) -> str:
    if summary is None:
        return "missing"
    return f"{summary['name']} ({summary['size']}, {summary['updated_at']})"


def _format_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d} ({seconds:.3f}s)"


def _format_seconds(seconds: float | None) -> str:
    return "disabled/unknown" if seconds is None else f"{seconds:.3f}s"


def _format_count(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _format_progress(progress: float | None) -> str:
    return "" if progress is None else f" ({progress * 100:.1f}%)"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
