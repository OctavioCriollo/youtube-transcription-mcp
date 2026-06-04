from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _require_non_empty(value: str, field_name: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    probability: float | None = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("word.start must be >= 0")
        if self.end < self.start:
            raise ValueError("word.end must be >= word.start")
        object.__setattr__(self, "text", _require_non_empty(self.text, "word.text"))
        if self.probability is not None and not 0 <= self.probability <= 1:
            raise ValueError("word.probability must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "probability": self.probability,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Word":
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data.get("text") or data.get("word") or ""),
            probability=(
                float(data["probability"])
                if data.get("probability") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = field(default_factory=tuple)
    speaker: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("segment.start must be >= 0")
        if self.end < self.start:
            raise ValueError("segment.end must be >= segment.start")
        object.__setattr__(self, "text", _require_non_empty(self.text, "segment.text"))
        object.__setattr__(self, "words", tuple(self.words))

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "words": [w.to_dict() for w in self.words],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data["text"]),
            speaker=data.get("speaker"),
            words=tuple(Word.from_dict(w) for w in data.get("words", [])),
        )


@dataclass(frozen=True)
class CanonicalTranscript:
    source: str
    provider: str
    model: str
    language: str
    duration: float
    segments: tuple[Segment, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "4.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _require_non_empty(self.source, "source"))
        object.__setattr__(self, "provider", _require_non_empty(self.provider, "provider"))
        object.__setattr__(self, "model", _require_non_empty(self.model, "model"))
        object.__setattr__(self, "language", _require_non_empty(self.language, "language"))
        if self.duration < 0:
            raise ValueError("duration must be >= 0")
        object.__setattr__(self, "segments", tuple(self.segments))

    @property
    def words(self) -> tuple[Word, ...]:
        flat: list[Word] = []
        for segment in self.segments:
            flat.extend(segment.words)
        return tuple(flat)

    @property
    def text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments if segment.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "provider": self.provider,
            "model": self.model,
            "language": self.language,
            "duration": self.duration,
            "created_at": self.created_at.isoformat(),
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalTranscript":
        created = data.get("created_at")
        created_at = (
            datetime.fromisoformat(created)
            if isinstance(created, str)
            else datetime.now(UTC)
        )
        return cls(
            source=str(data["source"]),
            provider=str(data["provider"]),
            model=str(data["model"]),
            language=str(data["language"]),
            duration=float(data["duration"]),
            created_at=created_at,
            schema_version=str(data.get("schema_version", "4.0")),
            segments=tuple(Segment.from_dict(s) for s in data.get("segments", [])),
        )


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    lines: tuple[str, ...]
    source_word_range: tuple[int, int]

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("cue.start must be >= 0")
        if self.end < self.start:
            raise ValueError("cue.end must be >= cue.start")
        if not self.lines:
            raise ValueError("cue.lines must not be empty")
        object.__setattr__(self, "lines", tuple(str(line) for line in self.lines))

    @property
    def text(self) -> str:
        return " ".join(line.strip() for line in self.lines if line.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "lines": list(self.lines),
            "source_word_range": list(self.source_word_range),
        }
