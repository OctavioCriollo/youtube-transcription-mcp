from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from transcription_v4.models import CanonicalTranscript, Segment, Word
from transcription_v4.text import smart_join


@dataclass(frozen=True)
class ChunkInfo:
    index: int
    nominal_start: float
    nominal_end: float
    actual_start: float
    overlap_left: float
    path: Path | None = None


def plan_chunks(
    *,
    duration_s: float,
    chunk_duration_s: float,
    overlap_s: float = 2.0,
) -> list[ChunkInfo]:
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if chunk_duration_s <= 0:
        raise ValueError("chunk_duration_s must be > 0")
    if overlap_s < 0:
        raise ValueError("overlap_s must be >= 0")

    chunks: list[ChunkInfo] = []
    nominal_start = 0.0
    index = 0
    while nominal_start < duration_s:
        nominal_end = min(duration_s, nominal_start + chunk_duration_s)
        actual_start = max(0.0, nominal_start - overlap_s)
        chunks.append(
            ChunkInfo(
                index=index,
                nominal_start=nominal_start,
                nominal_end=nominal_end,
                actual_start=actual_start,
                overlap_left=nominal_start - actual_start,
            )
        )
        if nominal_end >= duration_s:
            break
        nominal_start = nominal_end
        index += 1
    return chunks


def merge_chunk_transcripts(
    chunks: list[tuple[CanonicalTranscript, ChunkInfo]],
    *,
    source: str,
    provider: str,
    model: str,
    language: str,
    duration: float,
) -> CanonicalTranscript:
    if not chunks:
        raise ValueError("merge_chunk_transcripts requires at least one chunk")

    merged_entries: list[tuple[Word, str | None]] = []
    seen: set[tuple[str, int]] = set()
    for transcript, info in sorted(chunks, key=lambda item: item[1].index):
        for segment in transcript.segments:
            for word in segment.words:
                absolute = Word(
                    start=info.actual_start + word.start,
                    end=info.actual_start + word.end,
                    text=word.text,
                    probability=word.probability,
                )
                is_last = info.nominal_end >= duration
                if absolute.start < info.nominal_start:
                    continue
                if not is_last and absolute.start >= info.nominal_end:
                    continue
                dedup_key = (absolute.text.casefold().strip(), round(absolute.start * 10))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                merged_entries.append((absolute, segment.speaker))

    merged_entries.sort(key=lambda item: (item[0].start, item[0].end))
    segments = _word_entries_to_segments(merged_entries)
    return CanonicalTranscript(
        source=source,
        provider=provider,
        model=model,
        language=language,
        duration=duration,
        segments=tuple(segments),
    )


def _word_entries_to_segments(
    entries: list[tuple[Word, str | None]],
    *,
    max_gap_s: float = 1.0,
) -> list[Segment]:
    if not entries:
        return []
    groups: list[list[tuple[Word, str | None]]] = []
    current: list[tuple[Word, str | None]] = [entries[0]]
    for entry in entries[1:]:
        word, speaker = entry
        previous_word, previous_speaker = current[-1]
        if word.start - previous_word.end > max_gap_s or speaker != previous_speaker:
            groups.append(current)
            current = [entry]
        else:
            current.append(entry)
    groups.append(current)

    segments: list[Segment] = []
    for group in groups:
        words = [entry[0] for entry in group]
        speaker = group[0][1]
        segments.append(
            Segment(
                start=words[0].start,
                end=words[-1].end,
                text=smart_join(word.text for word in words),
                words=tuple(words),
                speaker=speaker,
            )
        )
    return segments


def _words_to_segments(words: list[Word], *, max_gap_s: float = 1.0) -> list[Segment]:
    if not words:
        return []
    groups: list[list[Word]] = []
    current: list[Word] = [words[0]]
    for word in words[1:]:
        if word.start - current[-1].end > max_gap_s:
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    groups.append(current)

    return [
        Segment(
            start=group[0].start,
            end=group[-1].end,
            text=smart_join(word.text for word in group),
            words=tuple(group),
        )
        for group in groups
    ]
