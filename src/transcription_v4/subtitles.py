from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from transcription_v4.models import CanonicalTranscript, Segment, SubtitleCue, Word
from transcription_v4.text import smart_join, wrap_lines


class SubtitleGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubtitleConfig:
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_duration_s: float = 7.0
    min_duration_s: float = 0.833
    min_gap_s: float = 0.100
    timing_epsilon_s: float = 0.001

    @property
    def max_chars_per_cue(self) -> int:
        return self.max_chars_per_line * self.max_lines


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total_ms = round(seconds * 1000)
    hours = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    minutes = rem // 60_000
    rem %= 60_000
    secs = rem // 1000
    ms = rem % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(",", ".")


def format_marker(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"


@dataclass(frozen=True)
class _IndexedWord:
    index: int
    word: Word


class SubtitleBuilder:
    def __init__(
        self,
        config: SubtitleConfig | None = None,
        *,
        allow_estimated_subtitles: bool = False,
    ) -> None:
        self.config = config or SubtitleConfig()
        self.allow_estimated_subtitles = allow_estimated_subtitles

    def build(self, transcript: CanonicalTranscript) -> list[SubtitleCue]:
        if not transcript.segments:
            return []

        segments = self._segments_with_words(transcript)
        cues: list[SubtitleCue] = []
        next_word_index = 0
        for segment in segments:
            indexed = [
                _IndexedWord(index=i, word=w)
                for i, w in enumerate(segment.words, start=next_word_index)
            ]
            next_word_index += len(indexed)
            cues.extend(self._segment_to_cues(indexed))

        return self._enforce_timing(cues)

    def _segments_with_words(self, transcript: CanonicalTranscript) -> list[Segment]:
        if all(segment.words for segment in transcript.segments):
            return list(transcript.segments)
        missing_count = sum(1 for segment in transcript.segments if not segment.words)
        if not self.allow_estimated_subtitles:
            raise SubtitleGenerationError(
                "Subtitle generation requires word timestamps; "
                f"{missing_count} segment(s) have no aligned words. "
                "Use a provider/model with word timestamps or pass "
                "--allow-estimated-subtitles."
            )
        return [
            segment if segment.words else self._estimate_segment_words(segment)
            for segment in transcript.segments
        ]

    @staticmethod
    def _estimate_segment_words(segment: Segment) -> Segment:
        tokens = segment.text.split()
        if not tokens:
            return segment
        duration = max(segment.end - segment.start, 0.001)
        step = duration / len(tokens)
        words = tuple(
            Word(
                start=segment.start + i * step,
                end=segment.start + (i + 1) * step,
                text=token,
                probability=None,
            )
            for i, token in enumerate(tokens)
        )
        return Segment(
            start=segment.start,
            end=segment.end,
            text=segment.text,
            words=words,
            speaker=segment.speaker,
        )

    def _segment_to_cues(self, words: list[_IndexedWord]) -> list[SubtitleCue]:
        cues: list[SubtitleCue] = []
        current: list[_IndexedWord] = []
        for item in words:
            candidate = current + [item]
            if current and self._candidate_exceeds_limits(candidate):
                cues.append(self._cue_from_words(current))
                current = [item]
            else:
                current = candidate
        if current:
            cues.append(self._cue_from_words(current))
        return cues

    def _candidate_exceeds_limits(self, words: list[_IndexedWord]) -> bool:
        text = smart_join(item.word.text for item in words)
        lines = wrap_lines(text, self.config.max_chars_per_line)
        duration = words[-1].word.end - words[0].word.start
        return (
            len(text) > self.config.max_chars_per_cue
            or len(lines) > self.config.max_lines
            or duration > self.config.max_duration_s
        )

    def _cue_from_words(self, words: list[_IndexedWord]) -> SubtitleCue:
        text = smart_join(item.word.text for item in words)
        lines = tuple(wrap_lines(text, self.config.max_chars_per_line))
        return SubtitleCue(
            start=words[0].word.start,
            end=words[-1].word.end,
            lines=lines,
            source_word_range=(words[0].index, words[-1].index + 1),
        )

    def _enforce_timing(self, cues: list[SubtitleCue]) -> list[SubtitleCue]:
        if not cues:
            return []
        adjusted = self._cap_long_cues(self._merge_short_cues(cues))

        # First reserve the configured gap by shortening the previous cue when needed.
        gap_fixed: list[SubtitleCue] = []
        for i, cue in enumerate(adjusted):
            end = cue.end
            if i + 1 < len(adjusted):
                max_end = adjusted[i + 1].start - self.config.min_gap_s
                if end > max_end:
                    end = max(cue.start, max_end)
            gap_fixed.append(
                SubtitleCue(
                    start=cue.start,
                    end=max(cue.start, end),
                    lines=cue.lines,
                    source_word_range=cue.source_word_range,
                )
            )

        # Then extend short cues only when there is room before the next cue.
        final: list[SubtitleCue] = []
        for i, cue in enumerate(gap_fixed):
            end = cue.end
            min_end = cue.start + self.config.min_duration_s
            if end < min_end:
                if i + 1 < len(gap_fixed):
                    max_end = gap_fixed[i + 1].start - self.config.min_gap_s
                    candidate_end = min(min_end, max_end)
                else:
                    candidate_end = min_end
                if candidate_end > end:
                    end = candidate_end
            final.append(
                SubtitleCue(
                    start=cue.start,
                    end=max(cue.start, end),
                    lines=cue.lines,
                    source_word_range=cue.source_word_range,
                )
            )
        return final

    def _merge_short_cues(self, cues: list[SubtitleCue]) -> list[SubtitleCue]:
        merged = list(cues)
        i = 0
        while i < len(merged):
            cue = merged[i]
            if cue.end - cue.start + self.config.timing_epsilon_s >= self.config.min_duration_s:
                i += 1
                continue

            if i + 1 < len(merged) and self._can_merge(merged[i], merged[i + 1]):
                merged[i] = self._merged_cue(merged[i], merged[i + 1])
                del merged[i + 1]
                continue

            if i > 0 and self._can_merge(merged[i - 1], merged[i]):
                merged[i - 1] = self._merged_cue(merged[i - 1], merged[i])
                del merged[i]
                i = max(0, i - 1)
                continue

            i += 1
        return merged

    def _cap_long_cues(self, cues: list[SubtitleCue]) -> list[SubtitleCue]:
        capped: list[SubtitleCue] = []
        for cue in cues:
            end = cue.end
            max_end = cue.start + self.config.max_duration_s
            if end - cue.start > self.config.max_duration_s + self.config.timing_epsilon_s:
                end = max_end
            capped.append(
                SubtitleCue(
                    start=cue.start,
                    end=end,
                    lines=cue.lines,
                    source_word_range=cue.source_word_range,
                )
            )
        return capped

    def _can_merge(self, left: SubtitleCue, right: SubtitleCue) -> bool:
        text = smart_join([left.text, right.text])
        lines = wrap_lines(text, self.config.max_chars_per_line)
        duration = right.end - left.start
        return (
            len(text) <= self.config.max_chars_per_cue
            and len(lines) <= self.config.max_lines
            and duration <= self.config.max_duration_s + self.config.timing_epsilon_s
        )

    def _merged_cue(self, left: SubtitleCue, right: SubtitleCue) -> SubtitleCue:
        text = smart_join([left.text, right.text])
        return SubtitleCue(
            start=left.start,
            end=right.end,
            lines=tuple(wrap_lines(text, self.config.max_chars_per_line)),
            source_word_range=(left.source_word_range[0], right.source_word_range[1]),
        )


def render_transcript_txt(transcript: CanonicalTranscript) -> str:
    return transcript.text.strip() + "\n"


def render_transcript_timestamps_txt(transcript: CanonicalTranscript) -> str:
    lines = [
        f"{format_marker(segment.start)} {segment.text.strip()}"
        for segment in transcript.segments
    ]
    return "\n".join(lines).strip() + "\n"


def render_srt(cues: Iterable[SubtitleCue]) -> str:
    parts: list[str] = []
    for index, cue in enumerate(cues, start=1):
        parts.append(str(index))
        parts.append(f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}")
        parts.extend(cue.lines)
        parts.append("")
    return "\n".join(parts)


def render_vtt(cues: Iterable[SubtitleCue]) -> str:
    parts: list[str] = ["WEBVTT", ""]
    for cue in cues:
        parts.append(f"{format_vtt_time(cue.start)} --> {format_vtt_time(cue.end)}")
        parts.extend(cue.lines)
        parts.append("")
    return "\n".join(parts)
