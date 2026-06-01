from __future__ import annotations

import concurrent.futures
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcription_v4.diarization import group_segments_by_speaker_and_time
from transcription_v4.models import CanonicalTranscript, Segment, Word
from transcription_v4.text import smart_join


LOCAL_PROVIDER = "local"
ELEVENLABS_PROVIDER = "elevenlabs"
GROQ_PROVIDER = "groq"
ELEVENLABS_DEFAULT_MODEL = "scribe_v2"
GROQ_DEFAULT_MODEL = "whisper-large-v3-turbo"
ELEVENLABS_FILE_LIMIT_BYTES = 3_000_000_000
GROQ_FILE_LIMIT_BYTES = 25 * 1024 * 1024
ELEVENLABS_COST_PER_HOUR_USD = 0.22
GROQ_COST_PER_HOUR_USD = 0.04


class FasterWhisperNotInstalledError(RuntimeError):
    pass


class ElevenLabsProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class GroqProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    remote: bool
    requires_auth: bool
    model: str
    supports_local_file: bool
    supports_source_url: bool
    word_timestamps: bool
    speakers: bool
    diarization: bool
    max_file_size_bytes: int | None = None
    cost_per_hour_usd: float | None = None


def provider_capabilities(name: str) -> ProviderCapabilities:
    if name == LOCAL_PROVIDER:
        return ProviderCapabilities(
            name=LOCAL_PROVIDER,
            remote=False,
            requires_auth=False,
            model="faster-whisper",
            supports_local_file=True,
            supports_source_url=False,
            word_timestamps=True,
            speakers=False,
            diarization=False,
        )
    if name == ELEVENLABS_PROVIDER:
        return ProviderCapabilities(
            name=ELEVENLABS_PROVIDER,
            remote=True,
            requires_auth=True,
            model=ELEVENLABS_DEFAULT_MODEL,
            supports_local_file=True,
            supports_source_url=True,
            word_timestamps=True,
            speakers=True,
            diarization=True,
            max_file_size_bytes=ELEVENLABS_FILE_LIMIT_BYTES,
            cost_per_hour_usd=ELEVENLABS_COST_PER_HOUR_USD,
        )
    if name == GROQ_PROVIDER:
        return ProviderCapabilities(
            name=GROQ_PROVIDER,
            remote=True,
            requires_auth=True,
            model=GROQ_DEFAULT_MODEL,
            supports_local_file=True,
            supports_source_url=False,
            word_timestamps=True,
            speakers=False,
            diarization=False,
            max_file_size_bytes=GROQ_FILE_LIMIT_BYTES,
            cost_per_hour_usd=GROQ_COST_PER_HOUR_USD,
        )
    raise ValueError(f"unsupported provider: {name}")


def resolve_device_and_compute_type(
    device: str = "auto",
    compute_type: str = "auto",
) -> tuple[str, str]:
    if device == "auto":
        try:
            import torch  # type: ignore[import-not-found]

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


class FasterWhisperProvider:
    def __init__(
        self,
        *,
        model: str = "large-v3",
        device: str = "auto",
        compute_type: str = "auto",
        download_root: str | None = None,
    ) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FasterWhisperNotInstalledError(
                "faster-whisper is not installed. Install with: pip install -e ."
            ) from exc
        self.model_name = model
        self.device, self.compute_type = resolve_device_and_compute_type(device, compute_type)
        self._model = WhisperModel(
            model,
            device=self.device,
            compute_type=self.compute_type,
            download_root=download_root,
        )

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> CanonicalTranscript:
        segments_iter, info = self._model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )
        segments: list[Segment] = []
        for raw_segment in segments_iter:
            words = tuple(self._word_from_provider(w) for w in (raw_segment.words or []))
            text = (raw_segment.text or "").strip()
            if not text and words:
                text = " ".join(word.text for word in words)
            if not text:
                continue
            segments.append(
                Segment(
                    start=float(raw_segment.start),
                    end=float(raw_segment.end),
                    text=text,
                    words=words,
                )
            )
        return CanonicalTranscript(
            source=str(audio_path),
            provider="faster-whisper",
            model=self.model_name,
            language=str(getattr(info, "language", language or "unknown")),
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            segments=tuple(segments),
        )

    @staticmethod
    def _word_from_provider(raw_word: Any) -> Word:
        return Word(
            start=float(getattr(raw_word, "start")),
            end=float(getattr(raw_word, "end")),
            text=str(getattr(raw_word, "word", "") or "").strip(),
            probability=(
                float(getattr(raw_word, "probability"))
                if getattr(raw_word, "probability", None) is not None
                else None
            ),
        )


class ElevenLabsProvider:
    endpoint = "https://api.elevenlabs.io/v1/speech-to-text"

    def __init__(
        self,
        *,
        model: str = ELEVENLABS_DEFAULT_MODEL,
        timeout_s: float = 3600.0,
        tag_audio_events: bool = True,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model
        self.timeout_s = timeout_s
        self.tag_audio_events = tag_audio_events
        self._api_key_override = api_key

    def transcribe_file(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        diarize: bool = False,
        num_speakers: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
        progress_label: str = "[transcribe] remote file",
    ) -> CanonicalTranscript:
        audio_path = Path(audio_path)
        if not audio_path.exists() or not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        if audio_path.stat().st_size >= ELEVENLABS_FILE_LIMIT_BYTES:
            raise ElevenLabsProviderError(
                f"ElevenLabs local file limit exceeded: {audio_path}",
                kind="content_too_large",
                retryable=False,
            )
        payload = self._post(
            data=self._request_data(
                language=language,
                diarize=diarize,
                num_speakers=num_speakers,
            ),
            file_path=audio_path,
            progress_callback=progress_callback,
            progress_label=progress_label,
        )
        return self._normalize_response(payload, source=str(audio_path))

    def transcribe_url(
        self,
        source_url: str,
        *,
        language: str | None = None,
        diarize: bool = False,
        num_speakers: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
        progress_label: str = "[transcribe] remote source_url",
    ) -> CanonicalTranscript:
        source_url = source_url.strip()
        if not source_url:
            raise ValueError("source_url must not be empty")
        data = self._request_data(
            language=language,
            diarize=diarize,
            num_speakers=num_speakers,
        )
        data["source_url"] = source_url
        payload = self._post(
            data=data,
            file_path=None,
            progress_callback=progress_callback,
            progress_label=progress_label,
        )
        return self._normalize_response(payload, source=source_url)

    def _request_data(
        self,
        *,
        language: str | None,
        diarize: bool,
        num_speakers: int | None,
    ) -> dict[str, str]:
        data = {
            "model_id": self.model_name,
            "timestamps_granularity": "word",
            "diarize": _bool_form_value(diarize),
            "tag_audio_events": _bool_form_value(self.tag_audio_events),
        }
        if language:
            data["language_code"] = language
        if num_speakers is not None:
            if not 1 <= num_speakers <= 32:
                raise ValueError("num_speakers must be between 1 and 32")
            data["num_speakers"] = str(num_speakers)
        return data

    def _post(
        self,
        *,
        data: dict[str, str],
        file_path: Path | None,
        progress_callback: Callable[[str], None] | None = None,
        progress_label: str = "[transcribe] remote request",
    ) -> dict[str, Any]:
        if progress_callback is None:
            return self._post_once(data=data, file_path=file_path)

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._post_once, data=data, file_path=file_path)
            while True:
                try:
                    return future.result(timeout=30.0)
                except concurrent.futures.TimeoutError:
                    progress_callback(
                        f"{progress_label} waiting for ElevenLabs "
                        f"({_format_elapsed(time.monotonic() - started)} elapsed)"
                    )

    def _post_once(self, *, data: dict[str, str], file_path: Path | None) -> dict[str, Any]:
        api_key = self._api_key()
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ElevenLabsProviderError(
                "httpx is not installed. Install with: pip install -e .",
                kind="not_installed",
                retryable=False,
            ) from exc

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                if file_path is None:
                    response = client.post(
                        self.endpoint,
                        headers={"xi-api-key": api_key},
                        data=data,
                    )
                else:
                    with file_path.open("rb") as fh:
                        response = client.post(
                            self.endpoint,
                            headers={"xi-api-key": api_key},
                            data=data,
                            files={
                                "file": (
                                    file_path.name,
                                    fh,
                                    _content_type_for(file_path),
                                )
                            },
                        )
            if response.status_code >= 400:
                raise _map_elevenlabs_status(response.status_code, response.text)
            return dict(response.json())
        except ElevenLabsProviderError:
            raise
        except TimeoutError as exc:
            raise ElevenLabsProviderError(
                f"ElevenLabs timeout: {exc}",
                kind="timeout",
                retryable=True,
            ) from exc
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                raise ElevenLabsProviderError(
                    f"ElevenLabs timeout: {exc}",
                    kind="timeout",
                    retryable=True,
                ) from exc
            raise ElevenLabsProviderError(
                f"ElevenLabs request failed: {exc}",
                kind="transient",
                retryable=True,
            ) from exc

    def _api_key(self) -> str:
        key = self._api_key_override or _load_api_key("elevenlabs", "ELEVENLABS_API_KEY")
        if not key:
            raise ElevenLabsProviderError(
                (
                    "ELEVENLABS_API_KEY is not configured. Set the env var or create "
                    "storage/secrets/elevenlabs.key"
                ),
                kind="auth",
                retryable=False,
            )
        return key

    def _normalize_response(self, payload: dict[str, Any], *, source: str) -> CanonicalTranscript:
        return normalize_elevenlabs_response(
            payload,
            source=source,
            provider=ELEVENLABS_PROVIDER,
            model=self.model_name,
        )


class GroqProvider:
    endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(
        self,
        *,
        model: str = GROQ_DEFAULT_MODEL,
        timeout_s: float = 600.0,
        api_key: str | None = None,
        max_file_size_bytes: int = GROQ_FILE_LIMIT_BYTES,
    ) -> None:
        self.model_name = model
        self.timeout_s = timeout_s
        self._api_key_override = api_key
        self.max_file_size_bytes = max_file_size_bytes

    def transcribe_file(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
        progress_label: str = "[transcribe] groq file",
    ) -> CanonicalTranscript:
        audio_path = Path(audio_path)
        if not audio_path.exists() or not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        if audio_path.stat().st_size >= self.max_file_size_bytes:
            raise GroqProviderError(
                f"Groq local file limit exceeded: {audio_path}",
                kind="content_too_large",
                retryable=False,
            )
        payload = self._post(
            file_path=audio_path,
            language=language,
            progress_callback=progress_callback,
            progress_label=progress_label,
        )
        return normalize_groq_response(
            payload,
            source=str(audio_path),
            provider=GROQ_PROVIDER,
            model=self.model_name,
        )

    def _post(
        self,
        *,
        file_path: Path,
        language: str | None,
        progress_callback: Callable[[str], None] | None = None,
        progress_label: str = "[transcribe] groq request",
    ) -> dict[str, Any]:
        if progress_callback is None:
            return self._post_once(file_path=file_path, language=language)

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._post_once, file_path=file_path, language=language)
            while True:
                try:
                    return future.result(timeout=30.0)
                except concurrent.futures.TimeoutError:
                    progress_callback(
                        f"{progress_label} waiting for Groq "
                        f"({_format_elapsed(time.monotonic() - started)} elapsed)"
                    )

    def _post_once(self, *, file_path: Path, language: str | None) -> dict[str, Any]:
        api_key = self._api_key()
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GroqProviderError(
                "httpx is not installed. Install with: pip install -e .",
                kind="not_installed",
                retryable=False,
            ) from exc

        timeout = httpx.Timeout(
            self.timeout_s,
            connect=min(60.0, self.timeout_s),
            read=self.timeout_s,
            write=self.timeout_s,
            pool=min(60.0, self.timeout_s),
        )
        try:
            with httpx.Client(timeout=timeout) as client:
                with file_path.open("rb") as fh:
                    response = client.post(
                        self.endpoint,
                        headers={"Authorization": f"Bearer {api_key}"},
                        files=[
                            ("file", (file_path.name, fh, _content_type_for(file_path))),
                            ("model", (None, self.model_name)),
                            ("response_format", (None, "verbose_json")),
                            ("temperature", (None, "0")),
                            ("timestamp_granularities[]", (None, "segment")),
                            ("timestamp_granularities[]", (None, "word")),
                            *(
                                [("language", (None, language))]
                                if language
                                else []
                            ),
                        ],
                    )
            if response.status_code >= 400:
                raise _map_groq_status(response.status_code, response.text)
            return dict(response.json())
        except GroqProviderError:
            raise
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                raise GroqProviderError(
                    f"Groq timeout: {exc}",
                    kind="timeout",
                    retryable=True,
                ) from exc
            raise GroqProviderError(
                f"Groq request failed: {exc}",
                kind="transient",
                retryable=True,
            ) from exc

    def _api_key(self) -> str:
        key = self._api_key_override or _load_api_key("groq", "GROQ_API_KEY")
        if not key:
            raise GroqProviderError(
                (
                    "GROQ_API_KEY is not configured. Set the env var or create "
                    "storage/secrets/groq.key"
                ),
                kind="auth",
                retryable=False,
            )
        return key


def normalize_elevenlabs_response(
    payload: dict[str, Any],
    *,
    source: str,
    provider: str = ELEVENLABS_PROVIDER,
    model: str = ELEVENLABS_DEFAULT_MODEL,
) -> CanonicalTranscript:
    raw_words = payload.get("words") or []
    word_items = [
        word
        for word in raw_words
        if word.get("type", "word") == "word"
        and word.get("start") is not None
        and word.get("end") is not None
        and str(word.get("text") or "").strip()
    ]
    segments = group_segments_by_speaker_and_time(word_items)
    language = _normalize_language_code(str(payload.get("language_code") or "unknown"))
    duration = float(payload.get("audio_duration_secs") or 0.0)
    if not duration and segments:
        duration = segments[-1].end
    return CanonicalTranscript(
        source=source,
        provider=provider,
        model=model,
        language=language,
        duration=duration,
        segments=tuple(segments),
    )


def normalize_groq_response(
    payload: dict[str, Any],
    *,
    source: str,
    provider: str = GROQ_PROVIDER,
    model: str = GROQ_DEFAULT_MODEL,
) -> CanonicalTranscript:
    words = [_groq_word_from_raw(word) for word in payload.get("words") or []]
    segments = _groq_segments_from_payload(payload, words)
    language = _normalize_language_code(str(payload.get("language") or "unknown"))
    duration = float(payload.get("duration") or 0.0)
    if not duration:
        if segments:
            duration = segments[-1].end
        elif words:
            duration = words[-1].end
    return CanonicalTranscript(
        source=source,
        provider=provider,
        model=model,
        language=language,
        duration=duration,
        segments=tuple(segments),
    )


def _groq_word_from_raw(raw: dict[str, Any]) -> Word:
    text = str(raw.get("word") or raw.get("text") or "").strip()
    return Word(
        start=float(raw.get("start", 0.0)),
        end=float(raw.get("end", 0.0)),
        text=text,
        probability=(
            float(raw["probability"])
            if raw.get("probability") is not None
            else None
        ),
    )


def _groq_segments_from_payload(
    payload: dict[str, Any],
    words: list[Word],
) -> list[Segment]:
    raw_segments = payload.get("segments") or []
    if not raw_segments:
        return _words_to_segments(words)

    segment_specs: list[tuple[float, float, str]] = []
    for raw_segment in raw_segments:
        start = float(raw_segment.get("start", 0.0))
        end = float(raw_segment.get("end", 0.0))
        text = str(raw_segment.get("text") or "").strip()
        segment_specs.append((start, end, text))

    assigned_words = _assign_words_to_segments(words, segment_specs)
    assigned_word_count = sum(len(segment_words) for segment_words in assigned_words)

    segments: list[Segment] = []
    for (start, end, text), segment_words in zip(segment_specs, assigned_words):
        segment_words_tuple = tuple(segment_words)
        if segment_words_tuple:
            text = smart_join(word.text for word in segment_words_tuple)
            start = segment_words_tuple[0].start
            end = segment_words_tuple[-1].end
        if not text:
            continue
        segments.append(
            Segment(
                start=start,
                end=end,
                text=text,
                words=segment_words_tuple,
            )
        )

    if segments and words and (
        assigned_word_count != len(words) or any(not segment.words for segment in segments)
    ):
        return _words_to_segments(words)
    if segments:
        return segments
    return _words_to_segments(words)


def _assign_words_to_segments(
    words: list[Word],
    segment_specs: list[tuple[float, float, str]],
) -> list[list[Word]]:
    assigned: list[list[Word]] = [[] for _ in segment_specs]
    for word in words:
        segment_index = _best_segment_index_for_word(word, segment_specs)
        if segment_index is not None:
            assigned[segment_index].append(word)
    return assigned


def _best_segment_index_for_word(
    word: Word,
    segment_specs: list[tuple[float, float, str]],
    *,
    tolerance_s: float = 0.05,
) -> int | None:
    best_index: int | None = None
    best_score = 0.0
    best_distance = float("inf")
    word_midpoint = (word.start + word.end) / 2.0
    for index, (start, end, _text) in enumerate(segment_specs):
        score = _word_segment_alignment_score(word, start, end, tolerance_s=tolerance_s)
        if score <= 0:
            continue
        segment_midpoint = (start + end) / 2.0
        distance = abs(word_midpoint - segment_midpoint)
        if score > best_score or (score == best_score and distance < best_distance):
            best_index = index
            best_score = score
            best_distance = distance
    return best_index


def _word_segment_alignment_score(
    word: Word,
    segment_start: float,
    segment_end: float,
    *,
    tolerance_s: float,
) -> float:
    overlap = min(word.end, segment_end) - max(word.start, segment_start)
    if overlap > 0:
        return overlap
    gap = max(segment_start - word.end, word.start - segment_end, 0.0)
    if gap <= tolerance_s:
        return tolerance_s - gap
    return 0.0


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


def _normalize_language_code(value: str) -> str:
    mapping = {
        "spa": "es",
        "eng": "en",
        "por": "pt",
        "fra": "fr",
        "deu": "de",
        "ita": "it",
    }
    normalized = value.strip().lower()
    return mapping.get(normalized, normalized or "unknown")


def _bool_form_value(value: bool) -> str:
    return "true" if value else "false"


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }.get(suffix, "application/octet-stream")


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _map_elevenlabs_status(status_code: int, detail: str) -> ElevenLabsProviderError:
    if status_code in {401, 403}:
        return ElevenLabsProviderError(
            f"ElevenLabs auth failed ({status_code}): {detail}",
            kind="auth",
            retryable=False,
        )
    if status_code == 413:
        return ElevenLabsProviderError(
            f"ElevenLabs content too large: {detail}",
            kind="content_too_large",
            retryable=False,
        )
    if status_code == 429:
        return ElevenLabsProviderError(
            f"ElevenLabs rate limit: {detail}",
            kind="rate_limit",
            retryable=True,
        )
    if status_code in {400, 404, 422}:
        return ElevenLabsProviderError(
            f"ElevenLabs rejected content ({status_code}): {detail}",
            kind="content_rejected",
            retryable=False,
        )
    if status_code >= 500:
        return ElevenLabsProviderError(
            f"ElevenLabs transient error ({status_code}): {detail}",
            kind="transient",
            retryable=True,
        )
    return ElevenLabsProviderError(
        f"ElevenLabs error ({status_code}): {detail}",
        kind="unknown",
        retryable=False,
    )


def _map_groq_status(status_code: int, detail: str) -> GroqProviderError:
    if status_code in {401, 403}:
        return GroqProviderError(
            f"Groq auth failed ({status_code}): {detail}",
            kind="auth",
            retryable=False,
        )
    if status_code == 413:
        return GroqProviderError(
            f"Groq content too large: {detail}",
            kind="content_too_large",
            retryable=False,
        )
    if status_code == 429:
        return GroqProviderError(
            f"Groq rate limit: {detail}",
            kind="rate_limit",
            retryable=True,
        )
    if status_code in {400, 404, 422}:
        return GroqProviderError(
            f"Groq rejected content ({status_code}): {detail}",
            kind="content_rejected",
            retryable=False,
        )
    if status_code >= 500:
        return GroqProviderError(
            f"Groq transient error ({status_code}): {detail}",
            kind="transient",
            retryable=True,
        )
    return GroqProviderError(
        f"Groq error ({status_code}): {detail}",
        kind="unknown",
        retryable=False,
    )


def _load_api_key(provider: str, env_name: str) -> str | None:
    env_value = os.environ.get(env_name)
    if env_value and env_value.strip():
        return env_value.strip()
    for path in _secret_file_candidates(provider):
        if not path.exists() or not path.is_file():
            continue
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def _secret_file_candidates(provider: str) -> list[Path]:
    relative = Path("storage") / "secrets" / f"{provider}.key"
    candidates: list[Path] = []

    configured = os.environ.get("TRANSCRIPTION_V4_SECRETS_DIR")
    if configured:
        return [(Path(configured) / f"{provider}.key").resolve()]

    project_root = Path(__file__).resolve().parents[2]
    candidates.append(project_root / relative)

    cwd = Path.cwd().resolve()
    candidates.append(cwd / relative)
    candidates.extend(parent / relative for parent in cwd.parents)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique
