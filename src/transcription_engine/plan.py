from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from transcription_engine.chunking import plan_chunks
from transcription_engine.media import FfmpegMedia
from transcription_engine.pipeline import (
    AUTO_CHUNK,
    DEFAULT_PROFILE,
    build_resume_criteria,
    describe_chunking_mode,
    describe_remote_chunking_mode,
    estimate_remote_cost_usd,
    find_resumable_run,
    normalize_chunk_duration_setting,
    resolve_chunk_duration_s,
    resolve_model,
    resolve_remote_chunk_duration_s,
)
from transcription_engine.providers import (
    ELEVENLABS_DEFAULT_MODEL,
    ELEVENLABS_COST_PER_HOUR_USD,
    ELEVENLABS_FILE_LIMIT_BYTES,
    ELEVENLABS_PROVIDER,
    GROQ_COST_PER_HOUR_USD,
    GROQ_DEFAULT_MODEL,
    GROQ_FILE_LIMIT_BYTES,
    GROQ_PROVIDER,
    LOCAL_PROVIDER,
    resolve_device_and_compute_type,
)
from transcription_engine.storage import FilesystemStorage, item_id_for_file


def plan_file(
    path: Path,
    *,
    storage_dir: Path = Path("storage"),
    provider: str = LOCAL_PROVIDER,
    model: str | None = None,
    profile: str = DEFAULT_PROFILE,
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = None,
    allow_estimated_subtitles: bool = False,
    chunk_duration_s: float | str | None = AUTO_CHUNK,
    overlap_s: float = 2.0,
    resume: bool = True,
    progress: bool = True,
    media: FfmpegMedia | None = None,
) -> dict[str, Any]:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)

    if provider == LOCAL_PROVIDER:
        resolved_model = resolve_model(model=model, profile=profile)
    elif provider == ELEVENLABS_PROVIDER:
        resolved_model = model or ELEVENLABS_DEFAULT_MODEL
    elif provider == GROQ_PROVIDER:
        resolved_model = model or GROQ_DEFAULT_MODEL
    else:
        raise ValueError(f"unsupported provider: {provider}")
    requested_chunk_duration_s = normalize_chunk_duration_setting(chunk_duration_s)
    resolved_device, resolved_compute_type = resolve_device_and_compute_type(
        device=device,
        compute_type=compute_type,
    )
    storage = FilesystemStorage(storage_dir)

    source_size = path.stat().st_size
    _log(
        f"[plan] hashing source file for stable item_id ({_format_bytes(source_size)})",
        progress=progress,
    )
    item_id = item_id_for_file(
        path,
        progress_callback=_HashProgressLogger(source_size, progress=progress),
    )
    _log(f"[plan] item_id={item_id}", progress=progress)

    media = media or FfmpegMedia()
    _log("[plan] probing media duration", progress=progress)
    duration_s = float(media.get_duration(path))
    _log(f"[plan] duration={duration_s:.3f}s", progress=progress)

    estimated_remote_audio_bytes = None
    remote_file_limit_bytes = None
    remote_cost_per_hour_usd = None
    if provider == ELEVENLABS_PROVIDER:
        remote_file_limit_bytes = ELEVENLABS_FILE_LIMIT_BYTES
        remote_cost_per_hour_usd = ELEVENLABS_COST_PER_HOUR_USD
        estimated_remote_audio_bytes = _estimate_remote_audio_bytes(duration_s, bitrate_kbps=128)
        resolved_chunk_duration_s = resolve_remote_chunk_duration_s(
            requested_chunk_duration_s,
            duration_s=duration_s,
            remote_input_size_bytes=estimated_remote_audio_bytes,
        )
        chunking_mode = describe_remote_chunking_mode(
            requested_chunk_duration_s,
            remote_input_size_bytes=estimated_remote_audio_bytes,
            resolved_chunk_duration_s=resolved_chunk_duration_s,
        )
    elif provider == GROQ_PROVIDER:
        remote_file_limit_bytes = GROQ_FILE_LIMIT_BYTES
        remote_cost_per_hour_usd = GROQ_COST_PER_HOUR_USD
        estimated_remote_audio_bytes = _estimate_remote_audio_bytes(duration_s, bitrate_kbps=128)
        resolved_chunk_duration_s = resolve_remote_chunk_duration_s(
            requested_chunk_duration_s,
            duration_s=duration_s,
            remote_input_size_bytes=estimated_remote_audio_bytes,
            file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
            auto_chunk_duration_s=10 * 60,
            provider_name="Groq",
        )
        chunking_mode = describe_remote_chunking_mode(
            requested_chunk_duration_s,
            remote_input_size_bytes=estimated_remote_audio_bytes,
            resolved_chunk_duration_s=resolved_chunk_duration_s,
            file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
        )
    else:
        resolved_chunk_duration_s = resolve_chunk_duration_s(
            requested_chunk_duration_s,
            duration_s=duration_s,
            device=resolved_device,
        )
        chunking_mode = describe_chunking_mode(
            requested_chunk_duration_s,
            resolved_chunk_duration_s=resolved_chunk_duration_s,
        )
    will_chunk = (
        resolved_chunk_duration_s is not None
        and duration_s > resolved_chunk_duration_s
    )
    chunks = (
        plan_chunks(
            duration_s=duration_s,
            chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
        )
        if will_chunk and resolved_chunk_duration_s is not None
        else []
    )

    resume_criteria = build_resume_criteria(
        path=path,
        provider=provider,
        profile=profile,
        model=resolved_model,
        device=device,
        compute_type=compute_type,
        language=language,
        chunk_duration_s=requested_chunk_duration_s,
        overlap_s=overlap_s,
        allow_estimated_subtitles=allow_estimated_subtitles,
    )
    resumable = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    item_dir = storage.root / "items" / item_id
    next_run_parent = item_dir / "runs"

    return {
        "schema_version": "4.0-plan",
        "source": {
            "path": str(path.resolve()),
            "size_bytes": source_size,
            "size": _format_bytes(source_size),
            "item_id": item_id,
            "duration_s": duration_s,
            "duration_hms": _format_duration(duration_s),
            "language": language,
        },
        "model": {
            "provider": provider,
            "profile": profile,
            "model": resolved_model,
            "requested_device": device,
            "requested_compute_type": compute_type,
            "resolved_device": resolved_device,
            "resolved_compute_type": resolved_compute_type,
            "allow_estimated_subtitles": allow_estimated_subtitles,
        },
        "remote": {
            "estimated_remote_audio_size_bytes": estimated_remote_audio_bytes,
            "estimated_remote_audio_size": (
                _format_bytes(estimated_remote_audio_bytes)
                if estimated_remote_audio_bytes is not None
                else None
            ),
            "file_limit_bytes": remote_file_limit_bytes,
            "estimated_cost_usd": (
                estimate_remote_cost_usd(
                    duration_s,
                    cost_per_hour_usd=remote_cost_per_hour_usd,
                )
                if provider in {ELEVENLABS_PROVIDER, GROQ_PROVIDER}
                else None
            ),
            "policy": (
                "extract audio and upload remote input; chunk only if needed"
                if provider == ELEVENLABS_PROVIDER
                else "extract audio and upload to Groq; chunk when file exceeds Groq limit"
                if provider == GROQ_PROVIDER
                else None
            ),
        },
        "chunking": {
            "requested_chunk_duration_s": requested_chunk_duration_s,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "will_chunk": will_chunk,
            "expected_chunks": len(chunks),
            "overlap_s": overlap_s,
            "first_chunk": _chunk_summary(chunks[0]) if chunks else None,
            "last_chunk": _chunk_summary(chunks[-1]) if chunks else None,
        },
        "storage": {
            "storage_dir": str(storage.root),
            "item_dir": str(item_dir),
            "runs_dir": str(next_run_parent),
        },
        "resume": {
            "enabled": resume,
            "would_resume": resumable is not None,
            "run_dir": str(resumable.run_dir) if resumable is not None else None,
        },
        "outputs": [
            "canonical.json",
            "transcript.txt",
            "transcript-timestamps.txt",
            "subtitles.srt",
            "subtitles.vtt",
            "quality.json",
            "audit.json",
            "audit.txt",
            "run.json",
        ],
        "warnings": _warnings(
            provider=provider,
            duration_s=duration_s,
            resolved_device=resolved_device,
            will_chunk=will_chunk,
            allow_estimated_subtitles=allow_estimated_subtitles,
        ),
        "next_action": _next_action(resumable_run=resumable.run_dir if resumable else None),
    }


def render_plan_text(report: dict[str, Any]) -> str:
    source = report["source"]
    model = report["model"]
    chunking = report["chunking"]
    remote = report.get("remote", {})
    storage = report["storage"]
    resume = report["resume"]
    warnings = report["warnings"]
    lines = [
        "Transcription engine plan",
        "",
        f"source: {source['path']}",
        f"size: {source['size']}",
        f"item_id: {source['item_id']}",
        f"duration: {source['duration_hms']}",
        f"language: {source['language'] or 'auto'}",
        "",
        "Model",
        f"- provider: {model.get('provider', 'local')}",
        f"- profile: {model['profile']}",
        f"- model: {model['model']}",
        f"- requested device: {model['requested_device']}",
        f"- resolved device: {model['resolved_device']}",
        f"- requested compute_type: {model['requested_compute_type']}",
        f"- resolved compute_type: {model['resolved_compute_type']}",
        "",
        "Chunking",
        f"- mode: {chunking['chunking_mode']}",
        f"- will chunk: {chunking['will_chunk']}",
        f"- requested chunk duration: {_format_seconds(chunking['requested_chunk_duration_s'])}",
        f"- resolved chunk duration: {_format_seconds(chunking['resolved_chunk_duration_s'])}",
        f"- expected chunks: {chunking['expected_chunks']}",
        f"- overlap: {chunking['overlap_s']:.3f}s",
    ]
    if remote.get("policy"):
        lines.extend(
            [
                "",
                "Remote",
                f"- policy: {remote['policy']}",
                f"- estimated remote audio: {remote['estimated_remote_audio_size']}",
                f"- file limit: {_format_bytes(remote['file_limit_bytes'])}",
                f"- estimated cost: USD {remote['estimated_cost_usd']:.4f}",
            ]
        )
    lines.extend(
        [
            "",
            "Storage",
            f"- storage_dir: {storage['storage_dir']}",
            f"- item_dir: {storage['item_dir']}",
            f"- runs_dir: {storage['runs_dir']}",
            "",
            "Resume",
            f"- enabled: {resume['enabled']}",
            f"- would resume: {resume['would_resume']}",
            f"- run_dir: {resume['run_dir'] or 'none'}",
        ]
    )
    if warnings:
        lines.extend(["", "Warnings"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", f"Next action: {report['next_action']}"])
    return "\n".join(lines).strip() + "\n"


def render_plan_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def _warnings(
    *,
    provider: str,
    duration_s: float,
    resolved_device: str,
    will_chunk: bool,
    allow_estimated_subtitles: bool,
) -> list[str]:
    warnings: list[str] = []
    if provider == LOCAL_PROVIDER and duration_s >= 3 * 60 * 60 and resolved_device == "cpu":
        warnings.append("long CPU transcription; expect a slow run")
    if provider == LOCAL_PROVIDER and not will_chunk and duration_s > 30 * 60:
        warnings.append("long media will run without chunks")
    if provider in {ELEVENLABS_PROVIDER, GROQ_PROVIDER}:
        warnings.append("remote provider may incur API costs")
    if allow_estimated_subtitles:
        warnings.append("estimated subtitles are enabled; prefer word timestamps")
    return warnings


def _next_action(*, resumable_run: Path | None) -> str:
    if resumable_run is not None:
        return "run the same file command to resume the incomplete run"
    return "run the file command to create a new run"


def _chunk_summary(chunk: Any) -> dict[str, float | int]:
    return {
        "index": chunk.index,
        "nominal_start": chunk.nominal_start,
        "nominal_end": chunk.nominal_end,
        "actual_start": chunk.actual_start,
        "overlap_left": chunk.overlap_left,
    }


def _estimate_remote_audio_bytes(duration_s: float, *, bitrate_kbps: int) -> int:
    return int((duration_s * bitrate_kbps * 1000) / 8)


def _log(message: str, *, progress: bool) -> None:
    if progress:
        print(message, flush=True)


class _HashProgressLogger:
    def __init__(
        self,
        total_bytes: int,
        *,
        progress: bool,
        min_interval_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.total_bytes = total_bytes
        self.progress = progress
        self.min_interval_bytes = min_interval_bytes
        self._next_report = min_interval_bytes

    def __call__(self, bytes_read: int) -> None:
        if not self.progress or self.total_bytes <= 0:
            return
        if bytes_read < self._next_report and bytes_read < self.total_bytes:
            return
        pct = min(100.0, bytes_read * 100 / self.total_bytes)
        _log(
            f"[plan] hashed {_format_bytes(bytes_read)} / "
            f"{_format_bytes(self.total_bytes)} ({pct:.1f}%)",
            progress=True,
        )
        while self._next_report <= bytes_read:
            self._next_report += self.min_interval_bytes


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d} ({seconds:.3f}s)"


def _format_seconds(value: Any) -> str:
    if value is None:
        return "disabled"
    if value == AUTO_CHUNK:
        return "auto"
    return f"{float(value):.3f}s"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")
