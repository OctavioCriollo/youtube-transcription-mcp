from __future__ import annotations

import pytest

from transcription_v4.models import CanonicalTranscript, Segment
from transcription_v4.providers import normalize_groq_response
from transcription_v4.quality import evaluate_quality
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


def test_normalize_groq_response_uses_words_as_canonical_text_source() -> None:
    payload = {
        "language": "spanish",
        "duration": 5.0,
        "segments": [
            {
                "start": 0.0,
                "end": 5.0,
                "text": "Tendr que hacerse cargo de su decisi s y explicar c funciona",
            },
        ],
        "words": [
            {"word": "Tendrá", "start": 0.2, "end": 0.5},
            {"word": "que", "start": 0.6, "end": 0.8},
            {"word": "hacerse", "start": 0.9, "end": 1.2},
            {"word": "cargo", "start": 1.3, "end": 1.6},
            {"word": "de", "start": 1.7, "end": 1.8},
            {"word": "su", "start": 1.9, "end": 2.0},
            {"word": "decisión", "start": 2.1, "end": 2.5},
            {"word": "sí", "start": 2.6, "end": 2.8},
            {"word": "y", "start": 2.9, "end": 3.0},
            {"word": "explicar", "start": 3.1, "end": 3.5},
            {"word": "cómo", "start": 3.6, "end": 3.9},
            {"word": "funciona", "start": 4.0, "end": 4.4},
        ],
    }

    transcript = normalize_groq_response(payload, source="audio.webm")
    cues = SubtitleBuilder().build(transcript)
    quality = evaluate_quality(transcript, cues)

    assert transcript.text == "Tendrá que hacerse cargo de su decisión sí y explicar cómo funciona"
    assert transcript.segments[0].start == 0.2
    assert transcript.segments[0].end == 4.4
    assert quality.status == "pass"


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
