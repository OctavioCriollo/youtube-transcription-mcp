from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcription_v4.audit import build_audit, render_audit_text
from transcription_v4.diarization import (
    build_speakers_report,
    render_diarized_txt,
    transcript_has_speakers,
)
from transcription_v4.models import CanonicalTranscript
from transcription_v4.quality import evaluate_quality
from transcription_v4.subtitles import (
    SubtitleBuilder,
    SubtitleConfig,
    render_srt,
    render_transcript_timestamps_txt,
    render_transcript_txt,
    render_vtt,
)


def regenerate_run_outputs(
    run_dir: Path,
    *,
    allow_estimated_subtitles: bool | None = None,
    config: SubtitleConfig | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    transcript = CanonicalTranscript.from_dict(
        json.loads((run_dir / "canonical.json").read_text(encoding="utf-8"))
    )
    run_path = run_dir / "run.json"
    run_state_path = run_dir / "run-state.json"
    run_record = json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else {}
    run_state = (
        json.loads(run_state_path.read_text(encoding="utf-8"))
        if run_state_path.exists()
        else {}
    )
    metadata = run_record.get("metadata", {}) or _metadata_from_run_state(run_state)
    metadata = _with_language_metadata(metadata, transcript=transcript)
    if allow_estimated_subtitles is None:
        allow_estimated_subtitles = bool(metadata.get("allow_estimated_subtitles", False))

    cfg = config or SubtitleConfig()
    cues = SubtitleBuilder(
        cfg,
        allow_estimated_subtitles=allow_estimated_subtitles,
    ).build(transcript)
    quality = evaluate_quality(
        transcript,
        cues,
        config=cfg,
        allow_estimated_subtitles=allow_estimated_subtitles,
    )
    srt = render_srt(cues)
    vtt = render_vtt(cues)
    audit = build_audit(transcript, cues, quality=quality, metadata=metadata, vtt_text=vtt)

    (run_dir / "transcript.txt").write_text(render_transcript_txt(transcript), encoding="utf-8")
    (run_dir / "transcript-timestamps.txt").write_text(
        render_transcript_timestamps_txt(transcript),
        encoding="utf-8",
    )
    (run_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
    (run_dir / "subtitles.vtt").write_text(vtt, encoding="utf-8")
    _write_json(run_dir / "quality.json", quality.to_dict())
    _write_json(run_dir / "audit.json", audit)
    (run_dir / "audit.txt").write_text(render_audit_text(audit), encoding="utf-8")
    has_speakers = transcript_has_speakers(transcript)
    if has_speakers:
        (run_dir / "diarized.txt").write_text(render_diarized_txt(transcript), encoding="utf-8")
        _write_json(run_dir / "speakers.json", build_speakers_report(transcript))

    run_record_created = not bool(run_record)
    if run_record_created:
        run_record = _new_run_record(run_dir, metadata=metadata)
    artifacts = run_record.setdefault("artifacts", {})
    artifacts.update(_artifact_map())
    if has_speakers:
        artifacts["diarized_txt"] = "diarized.txt"
        artifacts["speakers"] = "speakers.json"
    run_record["regenerated_at"] = datetime.now(UTC).isoformat()
    run_record["metadata"] = metadata
    _write_json(run_path, run_record)

    latest_written = _write_latest_if_storage_run(run_dir, run_record)
    if run_state_path.exists():
        run_state["status"] = "completed"
        _write_json(run_state_path, run_state)

    return {
        "run_dir": str(run_dir),
        "cue_count": len(cues),
        "quality_status": quality.status,
        "audit_status": audit["summary"]["status"],
        "run_record_created": run_record_created,
        "latest_written": latest_written,
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _metadata_from_run_state(run_state: dict[str, Any]) -> dict[str, Any]:
    if not run_state:
        return {}
    return {
        "source_path": run_state.get("source_path"),
        "profile": run_state.get("profile"),
        "model": run_state.get("model"),
        "device": run_state.get("resolved_device") or run_state.get("device"),
        "compute_type": run_state.get("resolved_compute_type") or run_state.get("compute_type"),
        "requested_device": run_state.get("device"),
        "requested_compute_type": run_state.get("compute_type"),
        "language": run_state.get("language"),
        "allow_estimated_subtitles": run_state.get("allow_estimated_subtitles", False),
        "chunk_duration_s": run_state.get("chunk_duration_s"),
        "resolved_chunk_duration_s": run_state.get("resolved_chunk_duration_s"),
        "chunking_mode": run_state.get("chunking_mode"),
        "overlap_s": run_state.get("overlap_s"),
        "resumed": True,
    }


def _with_language_metadata(
    metadata: dict[str, Any],
    *,
    transcript: CanonicalTranscript,
) -> dict[str, Any]:
    requested_language = metadata.get("requested_language", metadata.get("language"))
    return {
        **metadata,
        "language": transcript.language,
        "requested_language": requested_language,
        "detected_language": transcript.language,
    }


def _new_run_record(run_dir: Path, *, metadata: dict[str, Any]) -> dict[str, Any]:
    item_id = run_dir.parent.parent.name if run_dir.parent.name == "runs" else None
    return {
        "schema_version": "4.0-run",
        "item_id": item_id,
        "run_id": run_dir.name,
        "created_at": datetime.now(UTC).isoformat(),
        "artifacts": {"canonical": "canonical.json"},
        "metadata": metadata,
    }


def _artifact_map() -> dict[str, str]:
    return {
        "canonical": "canonical.json",
        "transcript_txt": "transcript.txt",
        "transcript_timestamps_txt": "transcript-timestamps.txt",
        "subtitles_srt": "subtitles.srt",
        "subtitles_vtt": "subtitles.vtt",
        "quality": "quality.json",
        "audit_json": "audit.json",
        "audit_txt": "audit.txt",
    }


def _write_latest_if_storage_run(run_dir: Path, run_record: dict[str, Any]) -> bool:
    if run_dir.parent.name != "runs":
        return False
    item_dir = run_dir.parent.parent
    item_id = run_record.get("item_id")
    if not item_id:
        return False
    latest_path = item_dir / "latest.json"
    _write_json(
        latest_path,
        {
            "schema_version": "4.0-latest",
            "item_id": item_id,
            "run_id": run_dir.name,
            "run_dir": str(run_dir.relative_to(item_dir)).replace("\\", "/"),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    return True
