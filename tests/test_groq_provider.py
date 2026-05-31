from __future__ import annotations

import pytest

from transcription_v4.models import CanonicalTranscript, Segment
from transcription_v4.providers import normalize_groq_response
from transcription_v4.subtitles import SubtitleBuilder, SubtitleGenerationError


def test_normalize_groq_response_assigns_cross_boundary_word_once() -> None:
    payload = {
        "language": "spanish",
        "duration": 3.0,
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "previo"},
            {"start": 1.0, "end": 2.0, "text": "cruza"},
            {"start": 2.0, "end": 3.0, "text": "final"},
        ],
        "words": [
            {"word": "previo", "start": 0.1, "end": 0.5},
            {"word": "cruza", "start": 0.8, "end": 1.7},
            {"word": "final", "start": 2.1, "end": 2.5},
        ],
    }

    transcript = normalize_groq_response(payload, source="audio.webm")

    assert [word.text for word in transcript.words] == ["previo", "cruza", "final"]
    assert [word.text for word in transcript.segments[0].words] == ["previo"]
    assert [word.text for word in transcript.segments[1].words] == ["cruza"]
    assert [word.text for word in transcript.segments[2].words] == ["final"]


def test_normalize_groq_response_falls_back_to_word_segments_if_alignment_loses_words() -> None:
    payload = {
        "language": "spanish",
        "duration": 3.0,
        "segments": [
            {"start": 1.0, "end": 1.5, "text": "segmento tarde"},
        ],
        "words": [
            {"word": "palabra", "start": 0.1, "end": 0.5},
        ],
    }

    transcript = normalize_groq_response(payload, source="audio.webm")

    assert [segment.text for segment in transcript.segments] == ["palabra"]
    assert [word.text for word in transcript.words] == ["palabra"]


def test_subtitle_error_reports_unaligned_segment_count() -> None:
    transcript = CanonicalTranscript(
        source="audio.webm",
        provider="groq",
        model="whisper-large-v3-turbo",
        language="es",
        duration=1.0,
        segments=(Segment(start=0.0, end=1.0, text="sin palabras"),),
    )

    with pytest.raises(SubtitleGenerationError, match="1 segment"):
        SubtitleBuilder().build(transcript)
