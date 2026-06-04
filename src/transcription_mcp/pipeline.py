"""MCP transcription orchestration.

The default YouTube path remains Groq -> ElevenLabs -> YouTube captions.
Additional providers and source types are explicit opt-ins.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from transcription_v4.pipeline import (
    transcribe_file as v4_transcribe_file,
    transcribe_youtube as v4_transcribe_youtube,
)
from transcription_v4.providers import ELEVENLABS_PROVIDER, GROQ_PROVIDER, LOCAL_PROVIDER
from transcription_v4.storage import FilesystemStorage, item_id_for_file, item_id_for_url
from transcription_v4.youtube import YtDlpYoutubeDownloader
from transcription_v4.models import CanonicalTranscript, Segment, SubtitleCue
from transcription_v4.quality import evaluate_quality
from transcription_v4.subtitles import SubtitleConfig
from transcription_v4.text import wrap_lines

from transcription_mcp.config import STORAGE_DIR_NAME
from transcription_mcp.youtube_subtitles import (
    NoSubtitlesAvailable,
    fetch_subtitles_transcript,
)


logger = logging.getLogger("transcription_mcp.pipeline")

StatusCallback = Callable[[dict[str, Any]], None]

SUBTITLES_PROVIDER = "subtitles"
DEFAULT_YOUTUBE_PROVIDER_ORDER = (GROQ_PROVIDER, ELEVENLABS_PROVIDER, SUBTITLES_PROVIDER)
DEFAULT_MEDIA_PROVIDER_ORDER = (GROQ_PROVIDER, ELEVENLABS_PROVIDER)
SUPPORTED_PROVIDERS = {
    LOCAL_PROVIDER,
    GROQ_PROVIDER,
    ELEVENLABS_PROVIDER,
    SUBTITLES_PROVIDER,
}
FINAL_ARTIFACTS = (
    "canonical.json",
    "transcript.txt",
    "transcript-timestamps.txt",
    "subtitles.srt",
    "subtitles.vtt",
    "quality.json",
    "audit.json",
    "audit.txt",
)


class TranscriptionFailed(RuntimeError):
    """All requested transcription methods were exhausted without producing a result."""


def transcribe_youtube_sync(
    *,
    url: str,
    language: str | None,
    workspace_dir: Path,
    provider_order: str | Iterable[str] | None = None,
    diarize: bool = False,
    num_speakers: int | None = None,
    ytdlp_cookies_file: Path | None = None,
    ytdlp_proxy: str | None = None,
    cache_ttl_hours: float | None = 24.0,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    providers = parse_provider_order(provider_order, allow_subtitles=True)
    cached = _read_cached_url_result(
        url=url,
        workspace_dir=workspace_dir,
        provider_order=providers,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        cache_ttl_hours=cache_ttl_hours,
    )
    if cached is not None:
        _emit_status(
            status_callback,
            stage="cache_hit",
            message="Returning cached completed transcription.",
            method=cached.get("method"),
            run_dir=cached.get("run_dir"),
        )
        return cached

    return _transcribe_url_chain(
        url=url,
        source_kind="youtube",
        workspace_dir=workspace_dir,
        providers=providers,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        ytdlp_cookies_file=ytdlp_cookies_file,
        ytdlp_proxy=ytdlp_proxy,
        allow_subtitles=True,
        status_callback=status_callback,
    )


def transcribe_media_url_sync(
    *,
    url: str,
    language: str | None,
    workspace_dir: Path,
    provider_order: str | Iterable[str] | None = None,
    diarize: bool = False,
    num_speakers: int | None = None,
    ytdlp_cookies_file: Path | None = None,
    ytdlp_proxy: str | None = None,
    cache_ttl_hours: float | None = 24.0,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    providers = parse_provider_order(provider_order, allow_subtitles=False)
    cached = _read_cached_url_result(
        url=url,
        workspace_dir=workspace_dir,
        provider_order=providers,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        cache_ttl_hours=cache_ttl_hours,
    )
    if cached is not None:
        _emit_status(
            status_callback,
            stage="cache_hit",
            message="Returning cached completed media URL transcription.",
            method=cached.get("method"),
            run_dir=cached.get("run_dir"),
        )
        return cached

    return _transcribe_url_chain(
        url=url,
        source_kind="media_url",
        workspace_dir=workspace_dir,
        providers=providers,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        ytdlp_cookies_file=ytdlp_cookies_file,
        ytdlp_proxy=ytdlp_proxy,
        allow_subtitles=False,
        status_callback=status_callback,
    )


def transcribe_file_sync(
    *,
    file_path: Path,
    language: str | None,
    workspace_dir: Path,
    provider_order: str | Iterable[str] | None = None,
    diarize: bool = False,
    num_speakers: int | None = None,
    cache_ttl_hours: float | None = 24.0,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    providers = parse_provider_order(provider_order, allow_subtitles=False)
    cached = _read_cached_file_result(
        path=path,
        workspace_dir=workspace_dir,
        provider_order=providers,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        cache_ttl_hours=cache_ttl_hours,
    )
    if cached is not None:
        _emit_status(
            status_callback,
            stage="cache_hit",
            message="Returning cached completed file transcription.",
            method=cached.get("method"),
            run_dir=cached.get("run_dir"),
        )
        return cached

    failed_attempts: dict[str, str] = {}
    for provider in providers:
        if provider == SUBTITLES_PROVIDER:
            continue
        if _skip_for_diarization(provider, diarize=diarize, num_speakers=num_speakers):
            failed_attempts[provider] = "Skipped: provider does not support diarization."
            continue
        _emit_status(
            status_callback,
            stage=f"{provider}_started",
            message=f"Trying {provider} file transcription.",
            method=provider,
            failed_attempts=failed_attempts.copy() or None,
        )
        try:
            run_dir = v4_transcribe_file(
                path,
                storage_dir=workspace_dir / STORAGE_DIR_NAME,
                provider=provider,
                language=language,
                diarize=diarize if provider == ELEVENLABS_PROVIDER else False,
                num_speakers=num_speakers if provider == ELEVENLABS_PROVIDER else None,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            failed_attempts[provider] = _describe_exception(exc)
            _emit_status(
                status_callback,
                stage=f"{provider}_failed",
                message=failed_attempts[provider],
                method=provider,
                failed_attempts=failed_attempts.copy(),
            )
            logger.warning("file transcription via %s failed: %s", provider, failed_attempts[provider])
            continue

        return _successful_result(
            run_dir=run_dir,
            method=provider,
            failed_attempts=failed_attempts,
            provider_order=providers,
            status_callback=status_callback,
        )

    raise _all_failed(failed_attempts)


def parse_provider_order(
    provider_order: str | Iterable[str] | None,
    *,
    allow_subtitles: bool,
) -> tuple[str, ...]:
    if provider_order is None:
        providers = DEFAULT_YOUTUBE_PROVIDER_ORDER if allow_subtitles else DEFAULT_MEDIA_PROVIDER_ORDER
    elif isinstance(provider_order, str):
        providers = tuple(part.strip().lower() for part in provider_order.split(",") if part.strip())
    else:
        providers = tuple(str(part).strip().lower() for part in provider_order if str(part).strip())

    if not providers:
        raise ValueError("provider_order must include at least one provider")

    unknown = [provider for provider in providers if provider not in SUPPORTED_PROVIDERS]
    if unknown:
        choices = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(f"unsupported provider(s): {', '.join(unknown)}; choices: {choices}")
    if not allow_subtitles and SUBTITLES_PROVIDER in providers:
        raise ValueError("subtitles provider is only valid for YouTube transcription")
    return providers


def _transcribe_url_chain(
    *,
    url: str,
    source_kind: str,
    workspace_dir: Path,
    providers: tuple[str, ...],
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
    ytdlp_cookies_file: Path | None,
    ytdlp_proxy: str | None,
    allow_subtitles: bool,
    status_callback: StatusCallback | None,
) -> dict[str, Any]:
    url = str(url).strip()
    if not url:
        raise ValueError("url must not be empty")

    failed_attempts: dict[str, str] = {}
    youtube_downloader = _youtube_downloader(
        cookies_file=ytdlp_cookies_file,
        proxy=ytdlp_proxy,
    )

    for provider in providers:
        if provider == SUBTITLES_PROVIDER:
            if diarize or num_speakers is not None:
                failed_attempts[provider] = "Skipped: provider does not support diarization."
                continue
            if not allow_subtitles:
                continue
            result = _try_subtitles_fallback(
                url=url,
                language=language,
                workspace_dir=workspace_dir,
                failed_attempts=failed_attempts,
                provider_order=providers,
                status_callback=status_callback,
            )
            if result is not None:
                return result
            continue

        if _skip_for_diarization(provider, diarize=diarize, num_speakers=num_speakers):
            failed_attempts[provider] = "Skipped: provider does not support diarization."
            continue

        _emit_status(
            status_callback,
            stage=f"{provider}_started",
            message=f"Trying {provider} transcription for {source_kind}.",
            method=provider,
            failed_attempts=failed_attempts.copy() or None,
        )
        try:
            run_dir = v4_transcribe_youtube(
                url,
                storage_dir=workspace_dir / STORAGE_DIR_NAME,
                provider=provider,
                language=language,
                diarize=diarize if provider == ELEVENLABS_PROVIDER else False,
                num_speakers=num_speakers if provider == ELEVENLABS_PROVIDER else None,
                youtube_downloader=youtube_downloader,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            failed_attempts[provider] = _describe_exception(exc)
            _emit_status(
                status_callback,
                stage=f"{provider}_failed",
                message=failed_attempts[provider],
                method=provider,
                failed_attempts=failed_attempts.copy(),
            )
            logger.warning("%s transcription via %s failed: %s", source_kind, provider, failed_attempts[provider])
            continue

        return _successful_result(
            run_dir=run_dir,
            method=provider,
            failed_attempts=failed_attempts,
            provider_order=providers,
            status_callback=status_callback,
        )

    raise _all_failed(failed_attempts)


def _try_subtitles_fallback(
    *,
    url: str,
    language: str | None,
    workspace_dir: Path,
    failed_attempts: dict[str, str],
    provider_order: tuple[str, ...],
    status_callback: StatusCallback | None,
) -> dict[str, Any] | None:
    _emit_status(
        status_callback,
        stage="subtitles_started",
        message="Trying YouTube captions fallback.",
        method=SUBTITLES_PROVIDER,
        failed_attempts=failed_attempts.copy() or None,
    )
    try:
        captions = fetch_subtitles_transcript(url, language=language)
        run_dir = _build_subtitles_run(
            url=url,
            language=language,
            captions=captions,
            workspace_dir=workspace_dir,
        )
    except (NoSubtitlesAvailable, ValueError) as exc:
        failed_attempts[SUBTITLES_PROVIDER] = _describe_exception(exc)
        _emit_status(
            status_callback,
            stage="subtitles_failed",
            message=failed_attempts[SUBTITLES_PROVIDER],
            method=SUBTITLES_PROVIDER,
            failed_attempts=failed_attempts.copy(),
        )
        return None

    # Same output contract as Groq/ElevenLabs: a real run_dir with the full
    # artifact manifest, so create_transcription_bundle and
    # get_transcription_artifact work for the subtitles path too.
    result = _read_run_artifacts(run_dir)
    result["method"] = SUBTITLES_PROVIDER
    result["provider_order_effective"] = list(provider_order)
    result["timestamp_level"] = captions.get("timestamp_level", "caption")
    result["word_timestamps"] = bool(captions.get("word_timestamps", False))
    result["cache"] = {"hit": False}
    if failed_attempts:
        result["failed_attempts"] = failed_attempts
    _emit_status(
        status_callback,
        stage="completed",
        message="Transcription completed with YouTube captions fallback.",
        method=SUBTITLES_PROVIDER,
        run_dir=str(run_dir),
        failed_attempts=failed_attempts.copy() or None,
    )
    return result


def _build_subtitles_run(
    *,
    url: str,
    language: str | None,
    captions: dict[str, Any],
    workspace_dir: Path,
) -> Path:
    """Persist YouTube captions as a normal run, reusing the shared storage writer.

    YouTube already segments captions into timed blocks, so each block maps directly
    to one canonical segment AND one subtitle cue (no word-level estimation). The
    same `FilesystemStorage.save_run` used by the audio providers writes transcript,
    timestamps, SRT/VTT, canonical, quality and audit artifacts.
    """
    raw_segments = captions.get("segments") or []
    config = SubtitleConfig()
    segments: list[Segment] = []
    cues: list[SubtitleCue] = []
    for index, block in enumerate(raw_segments):
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(block.get("start") or 0.0))
        end = max(start, float(block.get("end") or start))
        segments.append(Segment(start=start, end=end, text=text))
        lines = tuple(line for line in wrap_lines(text, config.max_chars_per_line) if line.strip())
        cues.append(
            SubtitleCue(
                start=start,
                end=end,
                lines=lines or (text,),
                source_word_range=(index, index + 1),
            )
        )
    if not segments:
        raise NoSubtitlesAvailable(f"no usable caption blocks for {url}")

    used_language = str(captions.get("language") or "unknown")
    transcript = CanonicalTranscript(
        source=url,
        provider="youtube-transcript-api",
        model=str(captions.get("model") or "youtube-captions"),
        language=used_language,
        duration=float(captions.get("duration_s") or segments[-1].end),
        segments=tuple(segments),
    )
    # Caption blocks have no word-level timestamps; allow_estimated keeps the
    # word_timestamps check at "warning" (honest) instead of "error".
    quality = evaluate_quality(
        transcript, cues, config=config, allow_estimated_subtitles=True
    )

    youtube = captions.get("youtube") or {}
    metadata = {
        "source_type": "youtube",
        "source_url": url,
        "provider": SUBTITLES_PROVIDER,
        "transcription_provider": SUBTITLES_PROVIDER,
        "requested_language": language,
        "detected_language": used_language,
        "language": used_language,
        "diarize": False,
        "num_speakers": None,
        "estimated_cost_usd": captions.get("estimated_cost_usd", 0.0),
        "timestamp_level": captions.get("timestamp_level", "caption"),
        "word_timestamps": bool(captions.get("word_timestamps", False)),
        "source_timestamps": captions.get("source_timestamps", "youtube_captions"),
        "youtube_video_id": youtube.get("video_id"),
        "youtube_title": youtube.get("title"),
        "youtube_channel": youtube.get("channel"),
    }

    storage = FilesystemStorage(Path(workspace_dir) / STORAGE_DIR_NAME)
    run_paths = storage.create_run(item_id=item_id_for_url(url))
    storage.save_run(
        run_paths,
        transcript=transcript,
        cues=cues,
        quality=quality,
        metadata=metadata,
    )
    return run_paths.run_dir


def _successful_result(
    *,
    run_dir: Path,
    method: str,
    failed_attempts: dict[str, str],
    provider_order: tuple[str, ...],
    status_callback: StatusCallback | None,
) -> dict[str, Any]:
    result = _read_run_artifacts(run_dir)
    result["method"] = method
    result["provider_order_effective"] = list(provider_order)
    result["cache"] = {"hit": False}
    if failed_attempts:
        result["failed_attempts"] = failed_attempts
    _emit_status(
        status_callback,
        stage="completed",
        message=f"Transcription completed with {method}.",
        method=method,
        run_dir=str(run_dir),
        failed_attempts=failed_attempts.copy() or None,
    )
    logger.info(
        "transcribed via %s: source=%s lang=%s chars=%d cost=%s",
        method,
        result.get("source", {}).get("url") or result.get("source", {}).get("path"),
        result.get("language"),
        len(result.get("transcript", "")),
        result.get("estimated_cost_usd"),
    )
    return result


def _read_cached_url_result(
    *,
    url: str,
    workspace_dir: Path,
    provider_order: tuple[str, ...],
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
    cache_ttl_hours: float | None,
) -> dict[str, Any] | None:
    if cache_ttl_hours is None:
        return None
    runs_dir = workspace_dir / STORAGE_DIR_NAME / "items" / item_id_for_url(url) / "runs"
    return _read_cached_result_from_runs(
        runs_dir=runs_dir,
        provider_order=provider_order,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        cache_ttl_hours=cache_ttl_hours,
        source_url=url,
        source_path=None,
    )


def _read_cached_file_result(
    *,
    path: Path,
    workspace_dir: Path,
    provider_order: tuple[str, ...],
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
    cache_ttl_hours: float | None,
) -> dict[str, Any] | None:
    if cache_ttl_hours is None:
        return None
    runs_dir = workspace_dir / STORAGE_DIR_NAME / "items" / item_id_for_file(path) / "runs"
    return _read_cached_result_from_runs(
        runs_dir=runs_dir,
        provider_order=provider_order,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        cache_ttl_hours=cache_ttl_hours,
        source_url=None,
        source_path=str(path),
    )


def _read_cached_result_from_runs(
    *,
    runs_dir: Path,
    provider_order: tuple[str, ...],
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
    cache_ttl_hours: float,
    source_url: str | None,
    source_path: str | None,
) -> dict[str, Any] | None:
    if not runs_dir.is_dir():
        return None

    # Cache serves only "real STT" results, never the degraded subtitles fallback:
    # subtitles is cheap to recompute and must never shadow a higher-priority
    # provider that may work now. So a cached subtitles run is ignored here.
    cacheable_order = tuple(p for p in provider_order if p != SUBTITLES_PROVIDER)
    if not cacheable_order:
        return None

    # Collect every valid, fresh, matching candidate, then pick by PRIORITY
    # (position in the order), not by recency. selected_provider already lives in
    # run.json (transcription_provider), so no extra metadata is needed.
    candidates: list[tuple[int, float, Path]] = []
    for run_dir in (path for path in runs_dir.iterdir() if path.is_dir()):
        run_json_path = run_dir / "run.json"
        if not run_json_path.exists() or not _final_artifacts_complete(run_dir):
            continue
        if not _cache_fresh(run_json_path, cache_ttl_hours=cache_ttl_hours):
            continue
        try:
            run_record = _read_json(run_json_path)
        except (OSError, json.JSONDecodeError):
            continue
        metadata = run_record.get("metadata", {}) or {}
        method = str(metadata.get("transcription_provider") or metadata.get("provider") or "")
        if method not in cacheable_order:
            continue
        if source_url is not None and metadata.get("source_url") != source_url:
            continue
        if source_path is not None and metadata.get("source_path") != source_path:
            continue
        if not _metadata_matches_options(
            metadata,
            language=language,
            diarize=diarize,
            num_speakers=num_speakers,
        ):
            continue
        candidates.append((cacheable_order.index(method), run_dir.stat().st_mtime, run_dir))

    if not candidates:
        return None

    # Lowest order-index wins (highest priority); break ties by most recent.
    _, _, run_dir = min(candidates, key=lambda item: (item[0], -item[1]))
    run_json_path = run_dir / "run.json"
    metadata = _read_json(run_json_path).get("metadata", {}) or {}
    method = str(metadata.get("transcription_provider") or metadata.get("provider") or "")
    result = _read_run_artifacts(run_dir)
    result["method"] = method
    result["provider_order_effective"] = list(provider_order)
    result["cache"] = {
        "hit": True,
        "run_dir": str(run_dir),
        "age_s": round(_age_seconds(run_json_path), 3),
        "ttl_hours": cache_ttl_hours,
    }
    return result


def _metadata_matches_options(
    metadata: dict[str, Any],
    *,
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
) -> bool:
    requested_language = metadata.get("requested_language")
    if requested_language != language:
        return False
    if bool(metadata.get("diarize", False)) != bool(diarize):
        return False
    if metadata.get("num_speakers") != num_speakers:
        return False
    return True


def _final_artifacts_complete(run_dir: Path) -> bool:
    return all((run_dir / artifact).exists() for artifact in FINAL_ARTIFACTS)


def _cache_fresh(path: Path, *, cache_ttl_hours: float) -> bool:
    return _age_seconds(path) <= cache_ttl_hours * 3600


def _age_seconds(path: Path) -> float:
    return datetime.now(UTC).timestamp() - path.stat().st_mtime


def _youtube_downloader(
    *,
    cookies_file: Path | None,
    proxy: str | None,
) -> YtDlpYoutubeDownloader | None:
    if cookies_file is None and not proxy:
        return None
    return YtDlpYoutubeDownloader(cookies_file=cookies_file, proxy=proxy)


def _skip_for_diarization(provider: str, *, diarize: bool, num_speakers: int | None) -> bool:
    return bool(diarize or num_speakers is not None) and provider != ELEVENLABS_PROVIDER


def _all_failed(failed_attempts: dict[str, str]) -> TranscriptionFailed:
    details = "\n".join(f"  - {provider}: {reason}" for provider, reason in failed_attempts.items())
    message = "All requested transcription methods failed."
    if details:
        message = f"{message}\n{details}"
    logger.error(message)
    return TranscriptionFailed(message)


def _emit_status(
    status_callback: StatusCallback | None,
    **event: Any,
) -> None:
    if status_callback is None:
        return
    try:
        status_callback({key: value for key, value in event.items() if value is not None})
    except Exception:  # noqa: BLE001
        logger.exception("transcription status callback failed")


def _describe_exception(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    if kind:
        return f"{type(exc).__name__}[{kind}]: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _read_run_artifacts(run_dir: Path) -> dict[str, Any]:
    transcript_text = (run_dir / "transcript.txt").read_text(encoding="utf-8").strip()
    canonical = _read_json(run_dir / "canonical.json")
    run_record = _read_json(run_dir / "run.json")
    audit = _read_json(run_dir / "audit.json")
    quality = _read_json(run_dir / "quality.json")

    metadata = run_record.get("metadata", {}) or {}
    result = {
        "transcript": transcript_text,
        "language": canonical.get("language"),
        "duration_s": canonical.get("duration"),
        "model": canonical.get("model"),
        "provider": canonical.get("provider"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "source": {
            "type": metadata.get("source_type"),
            "url": metadata.get("source_url"),
            "path": metadata.get("source_path"),
        },
        "youtube": {
            "video_id": metadata.get("youtube_video_id"),
            "title": metadata.get("youtube_title"),
            "channel": metadata.get("youtube_channel"),
        },
        "quality_status": quality.get("status"),
        "audit": {
            "status": audit.get("summary", {}).get("status"),
            "verdict": audit.get("summary", {}).get("verdict"),
        },
        "artifacts": _build_artifact_manifest(run_dir, run_record),
        "run_dir": str(run_dir),
    }
    speakers_path = run_dir / "speakers.json"
    if speakers_path.exists():
        result["speakers"] = _read_json(speakers_path)
    return result


def _build_artifact_manifest(run_dir: Path, run_record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts = dict(run_record.get("artifacts", {}) or {})
    artifacts.setdefault("run_json", "run.json")
    artifacts.setdefault("run_state", "run-state.json")
    manifest: dict[str, dict[str, Any]] = {}
    for name, relative_path in sorted(artifacts.items()):
        path = run_dir / str(relative_path)
        manifest[name] = {
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
        }
    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
