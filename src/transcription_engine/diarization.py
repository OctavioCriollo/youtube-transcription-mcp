from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from transcription_engine.models import CanonicalTranscript, Segment


@dataclass(frozen=True)
class SpeakerStats:
    speaker: str
    segments: int
    words: int
    speech_seconds: float
    first_start: float
    last_end: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "segments": self.segments,
            "words": self.words,
            "speech_seconds": round(self.speech_seconds, 3),
            "first_start": round(self.first_start, 3),
            "last_end": round(self.last_end, 3),
        }


def transcript_has_speakers(transcript: CanonicalTranscript) -> bool:
    return any(segment.speaker for segment in transcript.segments)


def render_diarized_txt(transcript: CanonicalTranscript) -> str:
    blocks: list[str] = []
    for segment in transcript.segments:
        if not segment.speaker:
            continue
        blocks.append(
            "[{start} --> {end}] {speaker}\n{text}".format(
                start=_format_timestamp(segment.start),
                end=_format_timestamp(segment.end),
                speaker=segment.speaker,
                text=segment.text.strip(),
            )
        )
    return "\n\n".join(blocks).strip() + ("\n" if blocks else "")


def build_speakers_report(transcript: CanonicalTranscript) -> dict[str, Any]:
    stats: dict[str, SpeakerStats] = {}
    for segment in transcript.segments:
        if not segment.speaker:
            continue
        current = stats.get(segment.speaker)
        word_count = len(segment.words)
        speech_seconds = max(0.0, segment.end - segment.start)
        if current is None:
            stats[segment.speaker] = SpeakerStats(
                speaker=segment.speaker,
                segments=1,
                words=word_count,
                speech_seconds=speech_seconds,
                first_start=segment.start,
                last_end=segment.end,
            )
            continue
        stats[segment.speaker] = SpeakerStats(
            speaker=segment.speaker,
            segments=current.segments + 1,
            words=current.words + word_count,
            speech_seconds=current.speech_seconds + speech_seconds,
            first_start=min(current.first_start, segment.start),
            last_end=max(current.last_end, segment.end),
        )

    speakers = sorted(stats.values(), key=lambda item: item.speaker)
    return {
        "schema_version": "4.0-speakers",
        "total_speakers": len(speakers),
        "speakers": [speaker.to_dict() for speaker in speakers],
    }


def normalize_speaker(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    lower = value.lower()
    for prefix in ("speaker_", "speaker "):
        if lower.startswith(prefix):
            suffix = lower[len(prefix) :].strip()
            if suffix.isdigit():
                return f"SPEAKER_{int(suffix):02d}"
    if lower.startswith("speaker") and lower[7:].strip().isdigit():
        return f"SPEAKER_{int(lower[7:].strip()):02d}"
    if len(value) == 1 and value.isalpha():
        return f"SPEAKER_{ord(value.upper()) - ord('A'):02d}"
    return value.upper()


def group_segments_by_speaker_and_time(
    words: list[dict[str, Any]],
    *,
    max_gap_s: float = 0.8,
    max_segment_duration_s: float = 12.0,
) -> list[Segment]:
    filtered = [
        word
        for word in words
        if word.get("type", "word") == "word"
        and word.get("text")
        and word.get("start") is not None
        and word.get("end") is not None
    ]
    if not filtered:
        return []

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_speaker: str | None = None
    for word in filtered:
        speaker = normalize_speaker(word.get("speaker_id"))
        if not current:
            current = [word]
            current_speaker = speaker
            continue

        prev_end = float(current[-1]["end"])
        start = float(word["start"])
        duration = prev_end - float(current[0]["start"])
        should_split = (
            speaker != current_speaker
            or start - prev_end > max_gap_s
            or duration >= max_segment_duration_s
        )
        if should_split:
            groups.append(current)
            current = [word]
            current_speaker = speaker
        else:
            current.append(word)
    if current:
        groups.append(current)

    from transcription_engine.models import Word
    from transcription_engine.text import smart_join

    segments: list[Segment] = []
    for group in groups:
        segment_words = tuple(
            Word(
                start=float(word["start"]),
                end=float(word["end"]),
                text=str(word["text"]).strip(),
                probability=None,
            )
            for word in group
            if str(word["text"]).strip()
        )
        if not segment_words:
            continue
        speaker = normalize_speaker(group[0].get("speaker_id"))
        segments.append(
            Segment(
                start=segment_words[0].start,
                end=segment_words[-1].end,
                text=smart_join(word.text for word in segment_words),
                words=segment_words,
                speaker=speaker,
            )
        )
    return segments


def _format_timestamp(value: float) -> str:
    milliseconds = int(round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
