from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from transcription_v4.audit import build_audit, render_audit_text
from transcription_v4.diarization import (
    build_speakers_report,
    render_diarized_txt,
    transcript_has_speakers,
)
from transcription_v4.models import CanonicalTranscript, SubtitleCue
from transcription_v4.quality import QualityReport
from transcription_v4.subtitles import (
    render_srt,
    render_transcript_timestamps_txt,
    render_transcript_txt,
    render_vtt,
)


HashProgressCallback = Callable[[int], None]


def sha256_file(path: Path, *, progress_callback: HashProgressCallback | None = None) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
            bytes_read += len(block)
            if progress_callback is not None:
                progress_callback(bytes_read)
    return digest.hexdigest()


def item_id_for_file(
    path: Path,
    *,
    progress_callback: HashProgressCallback | None = None,
) -> str:
    return f"file-{sha256_file(path, progress_callback=progress_callback)[:12]}"


def item_id_for_url(url: str) -> str:
    normalized = str(url).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"url-{digest[:12]}"


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("run_%Y%m%dT%H%M%S_%fZ")


@dataclass(frozen=True)
class RunPaths:
    item_id: str
    run_id: str
    item_dir: Path
    run_dir: Path
    latest_path: Path


class FilesystemStorage:
    def __init__(self, root: Path = Path("storage")) -> None:
        self.root = Path(root)

    def create_run(self, *, item_id: str, run_id: str | None = None) -> RunPaths:
        run_id = run_id or new_run_id()
        item_dir = self.root / "items" / item_id
        run_dir = item_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunPaths(
            item_id=item_id,
            run_id=run_id,
            item_dir=item_dir,
            run_dir=run_dir,
            latest_path=item_dir / "latest.json",
        )

    def save_run(
        self,
        paths: RunPaths,
        *,
        transcript: CanonicalTranscript,
        cues: list[SubtitleCue],
        quality: QualityReport,
        metadata: dict[str, Any],
    ) -> None:
        self._write_json(paths.run_dir / "canonical.json", transcript.to_dict())
        (paths.run_dir / "transcript.txt").write_text(
            render_transcript_txt(transcript),
            encoding="utf-8",
        )
        (paths.run_dir / "transcript-timestamps.txt").write_text(
            render_transcript_timestamps_txt(transcript),
            encoding="utf-8",
        )
        srt = render_srt(cues)
        vtt = render_vtt(cues)
        (paths.run_dir / "subtitles.srt").write_text(srt, encoding="utf-8")
        (paths.run_dir / "subtitles.vtt").write_text(vtt, encoding="utf-8")
        self._write_json(paths.run_dir / "quality.json", quality.to_dict())
        audit = build_audit(transcript, cues, quality=quality, metadata=metadata, vtt_text=vtt)
        self._write_json(paths.run_dir / "audit.json", audit)
        (paths.run_dir / "audit.txt").write_text(render_audit_text(audit), encoding="utf-8")

        artifacts = {
            "canonical": "canonical.json",
            "transcript_txt": "transcript.txt",
            "transcript_timestamps_txt": "transcript-timestamps.txt",
            "subtitles_srt": "subtitles.srt",
            "subtitles_vtt": "subtitles.vtt",
            "quality": "quality.json",
            "audit_json": "audit.json",
            "audit_txt": "audit.txt",
        }
        if transcript_has_speakers(transcript):
            (paths.run_dir / "diarized.txt").write_text(
                render_diarized_txt(transcript),
                encoding="utf-8",
            )
            self._write_json(paths.run_dir / "speakers.json", build_speakers_report(transcript))
            artifacts["diarized_txt"] = "diarized.txt"
            artifacts["speakers"] = "speakers.json"

        run_record = {
            "schema_version": "4.0-run",
            "item_id": paths.item_id,
            "run_id": paths.run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "artifacts": artifacts,
            "metadata": metadata,
        }
        self._write_json(paths.run_dir / "run.json", run_record)
        self._write_json(
            paths.latest_path,
            {
                "schema_version": "4.0-latest",
                "item_id": paths.item_id,
                "run_id": paths.run_id,
                "run_dir": str(paths.run_dir.relative_to(paths.item_dir)).replace("\\", "/"),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
