from __future__ import annotations

import json
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from transcription_engine.models import CanonicalTranscript, SubtitleCue
from transcription_engine.quality import QualityReport, evaluate_quality
from transcription_engine.subtitles import SubtitleConfig
from transcription_engine.text import token_counter, word_tokens


_SRT_CUE_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)"
)
_TIMESTAMP_RE = re.compile(r"(\d\d):(\d\d):(\d\d)[,.](\d\d\d)")
_SUSPICIOUS_UNICODE = {
    "replacement_char": re.compile("\ufffd"),
    "cyrillic": re.compile(r"[\u0400-\u04FF]"),
    "hangul": re.compile(r"[\uAC00-\uD7AF]"),
    "cjk": re.compile(r"[\u4E00-\u9FFF]"),
    "arabic": re.compile(r"[\u0600-\u06FF]"),
}


def audit_run(run_dir: Path, *, config: SubtitleConfig | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    transcript = CanonicalTranscript.from_dict(
        json.loads((run_dir / "canonical.json").read_text(encoding="utf-8"))
    )
    cues = parse_srt((run_dir / "subtitles.srt").read_text(encoding="utf-8"))
    metadata: dict[str, Any] = {}
    run_json = run_dir / "run.json"
    if run_json.exists():
        metadata = json.loads(run_json.read_text(encoding="utf-8")).get("metadata", {})
    vtt_text = (run_dir / "subtitles.vtt").read_text(encoding="utf-8")
    quality_path = run_dir / "quality.json"
    stored_quality = (
        json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else None
    )
    return build_audit(
        transcript,
        cues,
        metadata=metadata,
        vtt_text=vtt_text,
        stored_quality=stored_quality,
        config=config,
    )


def write_audit_files(run_dir: Path, report: dict[str, Any]) -> None:
    run_dir = Path(run_dir)
    (run_dir / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "audit.txt").write_text(render_audit_text(report), encoding="utf-8")


def build_audit(
    transcript: CanonicalTranscript,
    cues: list[SubtitleCue],
    *,
    metadata: dict[str, Any] | None = None,
    quality: QualityReport | None = None,
    stored_quality: dict[str, Any] | None = None,
    vtt_text: str | None = None,
    config: SubtitleConfig | None = None,
) -> dict[str, Any]:
    cfg = config or SubtitleConfig()
    metadata = metadata or {}
    quality_dict = quality.to_dict() if quality is not None else None
    if quality_dict is None:
        quality_dict = evaluate_quality(transcript, cues, config=cfg).to_dict()

    transcript_counter = token_counter(transcript.text)
    subtitle_counter = token_counter(" ".join(cue.text for cue in cues))
    vtt_counter = token_counter(_vtt_body_text(vtt_text)) if vtt_text is not None else None

    durations = [cue.end - cue.start for cue in cues]
    gaps = [cues[i].start - cues[i - 1].end for i in range(1, len(cues))]
    line_lengths = [len(line) for cue in cues for line in cue.lines]
    cue_word_counts = [len(word_tokens(cue.text)) for cue in cues]

    words = transcript.words
    probabilities = [word.probability for word in words if word.probability is not None]
    suspicious_unicode = _find_suspicious_unicode(transcript)

    return {
        "schema_version": "4.0-audit",
        "summary": _audit_summary(
            quality_dict=quality_dict,
            transcript_counter=transcript_counter,
            subtitle_counter=subtitle_counter,
            suspicious_unicode=suspicious_unicode,
            probabilities=probabilities,
        ),
        "metadata": metadata,
        "counts": {
            "segments": len(transcript.segments),
            "words": len(words),
            "transcript_tokens": sum(transcript_counter.values()),
            "subtitle_cues": len(cues),
            "subtitle_tokens": sum(subtitle_counter.values()),
            "vtt_tokens": sum(vtt_counter.values()) if vtt_counter is not None else None,
        },
        "parity": {
            "transcript_vs_srt_equal": transcript_counter == subtitle_counter,
            "missing_srt": _counter_sample(transcript_counter - subtitle_counter),
            "extra_srt": _counter_sample(subtitle_counter - transcript_counter),
            "transcript_vs_vtt_equal": (
                transcript_counter == vtt_counter if vtt_counter is not None else None
            ),
            "missing_vtt": (
                _counter_sample(transcript_counter - vtt_counter)
                if vtt_counter is not None
                else None
            ),
            "extra_vtt": (
                _counter_sample(vtt_counter - transcript_counter)
                if vtt_counter is not None
                else None
            ),
        },
        "quality": {
            "computed": quality_dict,
            "stored": stored_quality,
        },
        "subtitles": {
            "duration_s": _min_avg_max(durations),
            "gap_s": _min_avg_max(gaps),
            "line_chars": _min_avg_max(line_lengths),
            "cue_words": _min_avg_max(cue_word_counts),
            "overlap_count": sum(1 for gap in gaps if gap < -cfg.timing_epsilon_s),
            "short_cues": _short_cues(cues, cfg),
            "long_cues": _long_cues(cues, cfg),
        },
        "confidence": _confidence_report(transcript),
        "suspicious_unicode": suspicious_unicode,
        "long_segments": _long_segments(transcript),
        "large_silence_gaps": _large_word_gaps(transcript),
        "chunk_boundaries": _chunk_boundary_report(transcript, metadata),
        "repeated_word_runs": _repeated_word_runs(transcript),
    }


def parse_srt(text: str) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for match in _SRT_CUE_RE.finditer(text):
        lines = tuple(line.strip() for line in match.group(4).splitlines() if line.strip())
        if not lines:
            continue
        cues.append(
            SubtitleCue(
                start=_parse_timestamp(match.group(2)),
                end=_parse_timestamp(match.group(3)),
                lines=lines,
                source_word_range=(0, 0),
            )
        )
    return cues


def render_audit_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    counts = report["counts"]
    subtitles = report["subtitles"]
    confidence = report["confidence"]
    unicode_report = report["suspicious_unicode"]
    lines = [
        "Transcription engine audit",
        "",
        f"status: {summary['status']}",
        f"verdict: {summary['verdict']}",
        "",
        "Counts",
        f"- segments: {counts['segments']}",
        f"- words: {counts['words']}",
        f"- transcript tokens: {counts['transcript_tokens']}",
        f"- subtitle cues: {counts['subtitle_cues']}",
        f"- subtitle tokens: {counts['subtitle_tokens']}",
        "",
        "Parity",
        f"- transcript vs SRT: {report['parity']['transcript_vs_srt_equal']}",
        f"- transcript vs VTT: {report['parity']['transcript_vs_vtt_equal']}",
        "",
        "Subtitles",
        f"- duration min/avg/max: {subtitles['duration_s']}",
        f"- gap min/avg/max: {subtitles['gap_s']}",
        f"- line chars min/avg/max: {subtitles['line_chars']}",
        f"- overlap count: {subtitles['overlap_count']}",
        f"- short cue samples: {len(subtitles['short_cues'])}",
        f"- long cue samples: {len(subtitles['long_cues'])}",
        "",
        "Confidence",
        f"- probability min/avg/median/max: {confidence['probability_min_avg_median_max']}",
        f"- words probability < 0.30: {confidence['words_probability_lt_0_30']}",
        f"- words probability < 0.10: {confidence['words_probability_lt_0_10']}",
        "",
        "Suspicious Unicode",
        f"- total suspicious segments: {unicode_report['segment_count']}",
        f"- counts by kind: {unicode_report['counts_by_kind']}",
    ]
    return "\n".join(lines).strip() + "\n"


def _parse_timestamp(value: str) -> float:
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid subtitle timestamp: {value}")
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _vtt_body_text(text: str | None) -> str:
    if text is None:
        return ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "WEBVTT" or "-->" in stripped:
            continue
        lines.append(stripped)
    return " ".join(lines)


def _audit_summary(
    *,
    quality_dict: dict[str, Any],
    transcript_counter: Counter[str],
    subtitle_counter: Counter[str],
    suspicious_unicode: dict[str, Any],
    probabilities: list[float],
) -> dict[str, Any]:
    parity_ok = transcript_counter == subtitle_counter
    quality_status = quality_dict.get("status", "unknown")
    suspicious_count = suspicious_unicode["segment_count"]
    low_conf_ratio = (
        sum(1 for probability in probabilities if probability < 0.30) / len(probabilities)
        if probabilities
        else 0.0
    )
    if not parity_ok or quality_status == "error":
        status = "error"
        verdict = "generated artifacts are not reliable"
    elif suspicious_count or low_conf_ratio >= 0.10 or quality_status == "warning":
        status = "warning"
        verdict = "artifacts are complete, but transcript quality needs review"
    else:
        status = "pass"
        verdict = "artifacts passed structural and quality checks"
    return {
        "status": status,
        "verdict": verdict,
        "quality_status": quality_status,
        "low_confidence_ratio_lt_0_30": round(low_conf_ratio, 6),
        "suspicious_unicode_segments": suspicious_count,
    }


def _confidence_report(transcript: CanonicalTranscript) -> dict[str, Any]:
    probabilities = [word.probability for word in transcript.words if word.probability is not None]
    low_words = [
        {
            "start": round(word.start, 3),
            "end": round(word.end, 3),
            "text": word.text,
            "probability": round(float(word.probability), 4),
        }
        for word in transcript.words
        if word.probability is not None and word.probability < 0.30
    ]
    return {
        "probability_min_avg_median_max": _probability_min_avg_median_max(probabilities),
        "words_probability_lt_0_30": len(low_words),
        "words_probability_lt_0_10": sum(
            1
            for word in transcript.words
            if word.probability is not None and word.probability < 0.10
        ),
        "low_probability_samples": low_words[:50],
        "lowest_confidence_segments": _lowest_confidence_segments(transcript),
    }


def _probability_min_avg_median_max(values: list[float]) -> list[float] | None:
    if not values:
        return None
    return [
        round(min(values), 4),
        round(statistics.mean(values), 4),
        round(statistics.median(values), 4),
        round(max(values), 4),
    ]


def _find_suspicious_unicode(transcript: CanonicalTranscript) -> dict[str, Any]:
    counts_by_kind: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript.segments, start=1):
        kinds = [
            name for name, pattern in _SUSPICIOUS_UNICODE.items() if pattern.search(segment.text)
        ]
        if not kinds:
            continue
        counts_by_kind.update(kinds)
        samples.append(
            {
                "segment": index,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "kinds": kinds,
                "text": segment.text[:240],
            }
        )
    odd_characters: Counter[str] = Counter()
    for character in transcript.text:
        if character.isalnum() or character.isspace() or character in ".,;:!?%$+-()[]/\\'\"¿¡":
            continue
        odd_characters[f"{character} {unicodedata.name(character, 'UNKNOWN')}"] += 1
    return {
        "segment_count": len(samples),
        "counts_by_kind": dict(counts_by_kind),
        "samples": samples[:50],
        "odd_character_counts": dict(odd_characters.most_common(50)),
    }


def _short_cues(cues: list[SubtitleCue], cfg: SubtitleConfig) -> list[dict[str, Any]]:
    return [
        {
            "cue": index,
            "start": round(cue.start, 3),
            "end": round(cue.end, 3),
            "duration": round(cue.end - cue.start, 3),
            "text": cue.text,
        }
        for index, cue in enumerate(cues, start=1)
        if cue.end - cue.start + cfg.timing_epsilon_s < cfg.min_duration_s
    ][:50]


def _long_cues(cues: list[SubtitleCue], cfg: SubtitleConfig) -> list[dict[str, Any]]:
    return [
        {
            "cue": index,
            "start": round(cue.start, 3),
            "end": round(cue.end, 3),
            "duration": round(cue.end - cue.start, 3),
            "text": cue.text,
        }
        for index, cue in enumerate(cues, start=1)
        if cue.end - cue.start - cfg.timing_epsilon_s > cfg.max_duration_s
    ][:50]


def _long_segments(transcript: CanonicalTranscript, limit: int = 20) -> list[dict[str, Any]]:
    rows = sorted(
        transcript.segments,
        key=lambda segment: segment.end - segment.start,
        reverse=True,
    )
    return [
        {
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "duration": round(segment.end - segment.start, 3),
            "token_count": len(word_tokens(segment.text)),
            "text": segment.text[:240],
        }
        for segment in rows[:limit]
    ]


def _large_word_gaps(transcript: CanonicalTranscript, threshold_s: float = 30.0) -> list[dict[str, Any]]:
    words = transcript.words
    rows = []
    for previous, current in zip(words, words[1:]):
        gap = current.start - previous.end
        if gap >= threshold_s:
            rows.append(
                {
                    "gap": round(gap, 3),
                    "previous_end": round(previous.end, 3),
                    "next_start": round(current.start, 3),
                    "previous_word": previous.text,
                    "next_word": current.text,
                }
            )
    rows.sort(key=lambda item: item["gap"], reverse=True)
    return rows[:50]


def _chunk_boundary_report(
    transcript: CanonicalTranscript,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    chunk_duration = _numeric_chunk_duration(metadata)
    if chunk_duration is None:
        return []
    if chunk_duration <= 0:
        return []
    words = transcript.words
    boundaries = []
    boundary = chunk_duration
    while boundary < transcript.duration:
        near = [word for word in words if boundary - 5 <= word.start <= boundary + 5]
        duplicate_pairs = 0
        for i, left in enumerate(near):
            for right in near[i + 1 :]:
                if (
                    left.text.casefold().strip() == right.text.casefold().strip()
                    and abs(left.start - right.start) < 1.0
                ):
                    duplicate_pairs += 1
        boundaries.append(
            {
                "boundary_s": round(boundary, 3),
                "words_10s": len(near),
                "words_before_2s": " ".join(
                    word.text for word in words if boundary - 2 <= word.start < boundary
                ),
                "words_after_2s": " ".join(
                    word.text for word in words if boundary <= word.start < boundary + 2
                ),
                "near_duplicate_pairs": duplicate_pairs,
            }
        )
        boundary += chunk_duration
    return boundaries


def _numeric_chunk_duration(metadata: dict[str, Any]) -> float | None:
    for key in ("resolved_chunk_duration_s", "chunk_duration_s"):
        value = metadata.get(key)
        if value in (None, "", "auto", "off"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _repeated_word_runs(
    transcript: CanonicalTranscript,
    *,
    min_count: int = 4,
    limit: int = 50,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current_word = ""
    current_count = 0
    current_start = 0.0
    current_end = 0.0
    for word in transcript.words:
        normalized = re.sub(r"\W+", "", word.text.casefold())
        if normalized and normalized == current_word:
            current_count += 1
            current_end = word.end
            continue
        if current_word and current_count >= min_count:
            runs.append(
                {
                    "word": current_word,
                    "count": current_count,
                    "start": round(current_start, 3),
                    "end": round(current_end, 3),
                }
            )
        current_word = normalized
        current_count = 1
        current_start = word.start
        current_end = word.end
    if current_word and current_count >= min_count:
        runs.append(
            {
                "word": current_word,
                "count": current_count,
                "start": round(current_start, 3),
                "end": round(current_end, 3),
            }
        )
    return runs[:limit]


def _lowest_confidence_segments(
    transcript: CanonicalTranscript,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows: list[tuple[float, int, dict[str, Any]]] = []
    for index, segment in enumerate(transcript.segments, start=1):
        probabilities = [word.probability for word in segment.words if word.probability is not None]
        if not probabilities:
            continue
        average = statistics.mean(probabilities)
        rows.append(
            (
                average,
                index,
                {
                    "segment": index,
                    "start": round(segment.start, 3),
                    "end": round(segment.end, 3),
                    "average_probability": round(average, 4),
                    "word_count": len(probabilities),
                    "text": segment.text[:240],
                },
            )
        )
    rows.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in rows[:limit]]


def _counter_sample(counter: Counter[str], limit: int = 50) -> dict[str, int]:
    return dict(counter.most_common(limit))


def _min_avg_max(values: list[float] | list[int]) -> list[float] | None:
    if not values:
        return None
    return [
        round(float(min(values)), 3),
        round(float(statistics.mean(values)), 3),
        round(float(max(values)), 3),
    ]
