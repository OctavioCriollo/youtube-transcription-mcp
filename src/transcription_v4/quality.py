from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from transcription_v4.models import CanonicalTranscript, SubtitleCue
from transcription_v4.subtitles import SubtitleConfig
from transcription_v4.text import token_counter


@dataclass(frozen=True)
class QualityCheck:
    name: str
    status: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class QualityReport:
    status: str
    checks: tuple[QualityCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_quality(
    transcript: CanonicalTranscript,
    cues: list[SubtitleCue],
    *,
    config: SubtitleConfig | None = None,
    parity_threshold: float = 0.995,
    allow_estimated_subtitles: bool = False,
) -> QualityReport:
    cfg = config or SubtitleConfig()
    checks = [
        _check_parity(transcript, cues, parity_threshold),
        _check_cue_shape(cues, cfg),
        _check_timing(cues, cfg),
        _check_word_timestamps(
            transcript,
            allow_estimated_subtitles=allow_estimated_subtitles,
        ),
    ]
    if any(check.status == "error" for check in checks):
        status = "error"
    elif any(check.status == "warning" for check in checks):
        status = "warning"
    else:
        status = "pass"
    return QualityReport(status=status, checks=tuple(checks))


def _check_parity(
    transcript: CanonicalTranscript,
    cues: list[SubtitleCue],
    threshold: float,
) -> QualityCheck:
    transcript_counter = token_counter(transcript.text)
    subtitle_counter = token_counter(" ".join(cue.text for cue in cues))
    total = sum(transcript_counter.values())
    if total == 0:
        return QualityCheck("subtitle_token_parity", "error", {"reason": "empty transcript"})
    missing = transcript_counter - subtitle_counter
    extra = subtitle_counter - transcript_counter
    retained = 1.0 - (sum(missing.values()) / total)
    status = "pass" if retained >= threshold and not extra else "error"
    return QualityCheck(
        "subtitle_token_parity",
        status,
        {
            "retained_ratio": round(retained, 6),
            "threshold": threshold,
            "missing_count": sum(missing.values()),
            "extra_count": sum(extra.values()),
            "missing_sample": _counter_sample(missing),
            "extra_sample": _counter_sample(extra),
        },
    )


def _counter_sample(counter: Counter[str], limit: int = 20) -> dict[str, int]:
    return dict(counter.most_common(limit))


def _check_cue_shape(cues: list[SubtitleCue], cfg: SubtitleConfig) -> QualityCheck:
    violations: list[dict[str, Any]] = []
    for i, cue in enumerate(cues, start=1):
        if len(cue.lines) > cfg.max_lines:
            violations.append({"cue": i, "kind": "max_lines", "value": len(cue.lines)})
        for line in cue.lines:
            if len(line) > cfg.max_chars_per_line:
                violations.append({"cue": i, "kind": "cpl", "value": len(line)})
    return QualityCheck(
        "subtitle_shape",
        "pass" if not violations else "warning",
        {"violations": violations, "cue_count": len(cues)},
    )


def _check_timing(cues: list[SubtitleCue], cfg: SubtitleConfig) -> QualityCheck:
    violations: list[dict[str, Any]] = []
    previous_end: float | None = None
    eps = cfg.timing_epsilon_s
    for i, cue in enumerate(cues, start=1):
        duration = cue.end - cue.start
        if cue.start < 0 or cue.end < cue.start:
            violations.append({"cue": i, "kind": "invalid_range"})
        if duration + eps < cfg.min_duration_s:
            violations.append({"cue": i, "kind": "min_duration", "value": round(duration, 3)})
        if duration - eps > cfg.max_duration_s:
            violations.append({"cue": i, "kind": "max_duration", "value": round(duration, 3)})
        if previous_end is not None and cue.start - previous_end + eps < cfg.min_gap_s:
            violations.append(
                {
                    "cue": i,
                    "kind": "min_gap",
                    "value": round(cue.start - previous_end, 3),
                }
            )
        previous_end = cue.end
    return QualityCheck(
        "subtitle_timing",
        "pass" if not violations else "warning",
        {"violations": violations, "cue_count": len(cues)},
    )


def _check_word_timestamps(
    transcript: CanonicalTranscript,
    *,
    allow_estimated_subtitles: bool,
) -> QualityCheck:
    without_words = [i for i, segment in enumerate(transcript.segments) if not segment.words]
    if without_words and allow_estimated_subtitles:
        status = "warning"
    elif without_words:
        status = "error"
    else:
        status = "pass"
    return QualityCheck(
        "word_timestamps",
        status,
        {
            "segments_without_words": without_words[:50],
            "count": len(without_words),
            "estimated_subtitles_allowed": allow_estimated_subtitles,
        },
    )
