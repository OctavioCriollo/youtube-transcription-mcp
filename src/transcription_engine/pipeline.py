from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from transcription_engine.chunking import ChunkInfo, merge_chunk_transcripts, plan_chunks
from transcription_engine.media import FfmpegMedia
from transcription_engine.models import CanonicalTranscript
from transcription_engine.providers import (
    ELEVENLABS_COST_PER_HOUR_USD,
    ELEVENLABS_DEFAULT_MODEL,
    ELEVENLABS_FILE_LIMIT_BYTES,
    ELEVENLABS_PROVIDER,
    GROQ_COST_PER_HOUR_USD,
    GROQ_DEFAULT_MODEL,
    GROQ_FILE_LIMIT_BYTES,
    GROQ_PROVIDER,
    LOCAL_PROVIDER,
    ElevenLabsProvider,
    ElevenLabsProviderError,
    FasterWhisperProvider,
    GroqProvider,
    GroqProviderError,
    resolve_device_and_compute_type,
)
from transcription_engine.quality import evaluate_quality
from transcription_engine.storage import (
    FilesystemStorage,
    RunPaths,
    item_id_for_file,
    item_id_for_url,
    sha256_file,
)
from transcription_engine.subtitles import SubtitleBuilder, SubtitleConfig
from transcription_engine.youtube import (
    YtDlpYoutubeDownloader,
    YoutubeDownloadResult,
    ensure_yt_dlp_installed,
)


MODEL_PROFILES = {
    "draft": "tiny",
    "balanced": "medium",
    "quality": "large-v3",
}
DEFAULT_PROFILE = "balanced"

AUTO_CHUNK = "auto"
AUTO_CHUNK_THRESHOLD_S = 30 * 60
AUTO_CPU_CHUNK_DURATION_S = 15 * 60
AUTO_CUDA_CHUNK_DURATION_S = 30 * 60
AUTO_REMOTE_CHUNK_DURATION_S = 30 * 60
AUTO_GROQ_CHUNK_DURATION_S = 10 * 60
REMOTE_AUDIO_FORMATS = {"m4a", "mp3", "wav"}

ChunkDurationSetting = float | str | None


def transcribe_file(
    path: Path,
    *,
    storage_dir: Path = Path("storage"),
    provider: str = LOCAL_PROVIDER,
    model: str | None = None,
    profile: str = DEFAULT_PROFILE,
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = None,
    allow_estimated_subtitles: bool = False,
    chunk_duration_s: ChunkDurationSetting = AUTO_CHUNK,
    overlap_s: float = 2.0,
    resume: bool = True,
    progress: bool = True,
    diarize: bool = False,
    num_speakers: int | None = None,
    tag_audio_events: bool = True,
    provider_timeout_s: float = 3600.0,
    remote_audio_format: str = "m4a",
    remote_audio_bitrate_kbps: int = 128,
) -> Path:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)
    if provider == ELEVENLABS_PROVIDER:
        return _transcribe_file_elevenlabs(
            path,
            storage_dir=storage_dir,
            model=model,
            language=language,
            allow_estimated_subtitles=allow_estimated_subtitles,
            chunk_duration_s=chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
            diarize=diarize,
            num_speakers=num_speakers,
            tag_audio_events=tag_audio_events,
            provider_timeout_s=provider_timeout_s,
            remote_audio_format=remote_audio_format,
            remote_audio_bitrate_kbps=remote_audio_bitrate_kbps,
        )
    if provider == GROQ_PROVIDER:
        return _transcribe_file_groq(
            path,
            storage_dir=storage_dir,
            model=model,
            language=language,
            allow_estimated_subtitles=allow_estimated_subtitles,
            chunk_duration_s=chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
            diarize=diarize,
            num_speakers=num_speakers,
            provider_timeout_s=provider_timeout_s,
            remote_audio_format=remote_audio_format,
            remote_audio_bitrate_kbps=remote_audio_bitrate_kbps,
        )
    if provider != LOCAL_PROVIDER:
        raise ValueError(f"unsupported provider: {provider}")

    resolved_model = resolve_model(model=model, profile=profile)
    requested_chunk_duration_s = normalize_chunk_duration_setting(chunk_duration_s)
    resolved_device, resolved_compute_type = resolve_device_and_compute_type(
        device=device,
        compute_type=compute_type,
    )
    storage = FilesystemStorage(storage_dir)
    source_size = path.stat().st_size
    _log(
        f"[identify] hashing source file for stable item_id ({_format_bytes(source_size)})",
        progress=progress,
    )
    if source_size >= 1024 * 1024 * 1024:
        _log("[identify] large file; this can take a while", progress=progress)
    item_id = item_id_for_file(
        path,
        progress_callback=_HashProgressLogger(source_size, progress=progress),
    )
    _log(f"[identify] item_id={item_id}", progress=progress)
    resume_criteria = build_resume_criteria(
        path=path,
        provider=LOCAL_PROVIDER,
        profile=profile,
        model=resolved_model,
        device=device,
        compute_type=compute_type,
        language=language,
        chunk_duration_s=requested_chunk_duration_s,
        overlap_s=overlap_s,
        allow_estimated_subtitles=allow_estimated_subtitles,
    )
    run_state = {
        **resume_criteria,
        "source_type": "file",
        "resolved_device": resolved_device,
        "resolved_compute_type": resolved_compute_type,
    }
    run_paths = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    if run_paths is None:
        run_paths = storage.create_run(item_id=item_id)
        _log(f"[run] created {run_paths.run_dir}", progress=progress)
    else:
        _log(f"[run] resuming {run_paths.run_dir}", progress=progress)
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {**run_state, "status": "running"},
    )

    media = FfmpegMedia()
    media_dir = run_paths.run_dir / "media"
    prepared_audio = media_dir / "prepared.wav"
    if prepared_audio.exists():
        _log(f"[media] using existing prepared audio {prepared_audio}", progress=progress)
    else:
        _log("[media] extracting audio", progress=progress)
        media.extract_audio(
            path,
            prepared_audio,
            progress_callback=_progress_callback(progress),
        )
    duration = media.get_duration(prepared_audio)
    _log(f"[media] duration {duration:.3f}s", progress=progress)
    resolved_chunk_duration_s = resolve_chunk_duration_s(
        requested_chunk_duration_s,
        duration_s=duration,
        device=resolved_device,
    )
    chunking_mode = describe_chunking_mode(
        requested_chunk_duration_s,
        resolved_chunk_duration_s=resolved_chunk_duration_s,
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **run_state,
            "duration_s": duration,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "running",
        },
    )
    if resolved_chunk_duration_s is None:
        _log("[chunking] disabled", progress=progress)
    else:
        _log(
            f"[chunking] mode={chunking_mode} duration={resolved_chunk_duration_s:.0f}s",
            progress=progress,
        )

    _log(f"[provider] loading faster-whisper model={resolved_model}", progress=progress)
    provider = FasterWhisperProvider(
        model=resolved_model,
        device=resolved_device,
        compute_type=resolved_compute_type,
    )
    _log(
        f"[provider] ready device={provider.device} compute_type={provider.compute_type}",
        progress=progress,
    )
    if resolved_chunk_duration_s is not None and duration > resolved_chunk_duration_s:
        chunk_dir = media_dir / "chunks"
        chunks = _load_or_create_chunks(
            media,
            prepared_audio,
            chunk_dir,
            duration=duration,
            chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            progress=progress,
        )
        partial_dir = run_paths.run_dir / "partials"
        partial_dir.mkdir(parents=True, exist_ok=True)
        partials = []
        for position, chunk in enumerate(chunks, start=1):
            if chunk.path is None:
                raise RuntimeError("chunk path missing")
            partial_path = partial_dir / f"chunk_{chunk.index:04d}.canonical.json"
            if resume and partial_path.exists():
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} loading partial",
                    progress=progress,
                )
                partial = _load_transcript(partial_path)
            else:
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} started",
                    progress=progress,
                )
                partial = provider.transcribe(chunk.path, language=language)
                _write_json_atomic(partial_path, partial.to_dict())
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} saved partial",
                    progress=progress,
                )
            partials.append((partial, chunk))
        _log("[merge] merging chunks", progress=progress)
        transcript = merge_chunk_transcripts(
            partials,
            source=str(path),
            provider="faster-whisper",
            model=resolved_model,
            language=language or partials[0][0].language,
            duration=duration,
        )
    else:
        _log("[transcribe] full file started", progress=progress)
        transcript = provider.transcribe(prepared_audio, language=language)
        _log("[transcribe] full file finished", progress=progress)

    config = SubtitleConfig()
    _log("[subtitles] building SRT/VTT cues", progress=progress)
    cues = SubtitleBuilder(
        config,
        allow_estimated_subtitles=allow_estimated_subtitles,
    ).build(transcript)
    _log("[quality] evaluating artifacts", progress=progress)
    quality = evaluate_quality(
        transcript,
        cues,
        config=config,
        allow_estimated_subtitles=allow_estimated_subtitles,
    )

    _log("[outputs] writing final run artifacts", progress=progress)
    storage.save_run(
        run_paths,
        transcript=transcript,
        cues=cues,
        quality=quality,
        metadata={
            "source_path": str(path.resolve()),
            "source_type": "file",
            "provider": LOCAL_PROVIDER,
            "transcription_provider": "faster-whisper",
            "profile": profile,
            "model": resolved_model,
            "device": provider.device,
            "compute_type": provider.compute_type,
            "requested_device": device,
            "requested_compute_type": compute_type,
            **_language_metadata(requested_language=language, transcript=transcript),
            "allow_estimated_subtitles": allow_estimated_subtitles,
            "chunk_duration_s": requested_chunk_duration_s,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "overlap_s": overlap_s,
            "resumed": resume,
        },
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **run_state,
            "duration_s": duration,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "completed",
        },
    )
    _log(f"[done] {run_paths.run_dir}", progress=progress)
    return run_paths.run_dir


def transcribe_youtube(
    url: str,
    *,
    storage_dir: Path = Path("storage"),
    provider: str = ELEVENLABS_PROVIDER,
    profile: str = DEFAULT_PROFILE,
    model: str | None = None,
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = None,
    allow_estimated_subtitles: bool = False,
    chunk_duration_s: ChunkDurationSetting = AUTO_CHUNK,
    overlap_s: float = 2.0,
    resume: bool = True,
    progress: bool = True,
    diarize: bool = False,
    num_speakers: int | None = None,
    tag_audio_events: bool = True,
    provider_timeout_s: float = 3600.0,
    youtube_downloader: YtDlpYoutubeDownloader | None = None,
    media: FfmpegMedia | None = None,
    local_provider_factory: Callable[..., FasterWhisperProvider] | None = None,
    groq_provider_factory: Callable[..., GroqProvider] | None = None,
) -> Path:
    url = str(url).strip()
    if not url:
        raise ValueError("url must not be empty")
    if provider == LOCAL_PROVIDER:
        return _transcribe_youtube_local(
            url,
            storage_dir=storage_dir,
            profile=profile,
            model=model,
            device=device,
            compute_type=compute_type,
            language=language,
            allow_estimated_subtitles=allow_estimated_subtitles,
            chunk_duration_s=chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
            diarize=diarize,
            num_speakers=num_speakers,
            youtube_downloader=youtube_downloader,
            media=media,
            local_provider_factory=local_provider_factory,
        )
    if provider == GROQ_PROVIDER:
        return _transcribe_youtube_groq(
            url,
            storage_dir=storage_dir,
            model=model,
            language=language,
            allow_estimated_subtitles=allow_estimated_subtitles,
            chunk_duration_s=chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
            diarize=diarize,
            num_speakers=num_speakers,
            provider_timeout_s=provider_timeout_s,
            youtube_downloader=youtube_downloader,
            media=media,
            groq_provider_factory=groq_provider_factory,
        )
    if provider != ELEVENLABS_PROVIDER:
        raise ValueError(f"unsupported youtube provider: {provider}")

    resolved_model = model or ELEVENLABS_DEFAULT_MODEL
    storage = FilesystemStorage(storage_dir)
    item_id = item_id_for_url(url)
    resume_criteria = {
        "source_type": "youtube",
        "source_url": url,
        "provider": ELEVENLABS_PROVIDER,
        "model": resolved_model,
        "language": language,
        "allow_estimated_subtitles": allow_estimated_subtitles,
        "diarize": diarize,
        "num_speakers": num_speakers,
        "tag_audio_events": tag_audio_events,
    }
    run_paths = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    if run_paths is None:
        run_paths = storage.create_run(item_id=item_id)
        _log(f"[run] created {run_paths.run_dir}", progress=progress)
    else:
        _log(f"[run] resuming {run_paths.run_dir}", progress=progress)
    _write_json_atomic(run_paths.run_dir / "run-state.json", {**resume_criteria, "status": "running"})

    _log(f"[provider] loading elevenlabs model={resolved_model}", progress=progress)
    remote_provider = ElevenLabsProvider(
        model=resolved_model,
        timeout_s=provider_timeout_s,
        tag_audio_events=tag_audio_events,
    )
    _log("[provider] ready provider=elevenlabs mode=source_url", progress=progress)
    _log("[transcribe] source_url started", progress=progress)
    transcript = remote_provider.transcribe_url(
        url,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        progress_callback=_progress_callback(progress),
        progress_label="[transcribe] source_url",
    )
    _log("[transcribe] source_url finished", progress=progress)

    duration = transcript.duration
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {**resume_criteria, "duration_s": duration, "status": "running"},
    )
    _finalize_run(
        storage,
        run_paths,
        transcript=transcript,
        allow_estimated_subtitles=allow_estimated_subtitles,
        progress=progress,
        metadata={
            "source_type": "youtube",
            "source_url": url,
            "provider": ELEVENLABS_PROVIDER,
            "transcription_provider": ELEVENLABS_PROVIDER,
            "model": resolved_model,
            **_language_metadata(requested_language=language, transcript=transcript),
            "allow_estimated_subtitles": allow_estimated_subtitles,
            "diarize": diarize,
            "num_speakers": num_speakers,
            "tag_audio_events": tag_audio_events,
            "provider_timeout_s": provider_timeout_s,
            "remote_input": "source_url",
            "estimated_cost_usd": estimate_remote_cost_usd(duration),
            "chunking_mode": "provider-internal",
            "resumed": resume,
        },
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {**resume_criteria, "duration_s": duration, "status": "completed"},
    )
    _log(f"[done] {run_paths.run_dir}", progress=progress)
    return run_paths.run_dir


def _transcribe_youtube_local(
    url: str,
    *,
    storage_dir: Path,
    profile: str,
    model: str | None,
    device: str,
    compute_type: str,
    language: str | None,
    allow_estimated_subtitles: bool,
    chunk_duration_s: ChunkDurationSetting,
    overlap_s: float,
    resume: bool,
    progress: bool,
    diarize: bool,
    num_speakers: int | None,
    youtube_downloader: YtDlpYoutubeDownloader | None,
    media: FfmpegMedia | None,
    local_provider_factory: Callable[..., FasterWhisperProvider] | None,
) -> Path:
    if diarize or num_speakers is not None:
        raise ValueError("local YouTube transcription does not support diarization yet")
    if youtube_downloader is None:
        ensure_yt_dlp_installed()
        youtube_downloader = YtDlpYoutubeDownloader()

    resolved_model = resolve_model(model=model, profile=profile)
    requested_chunk_duration_s = normalize_chunk_duration_setting(chunk_duration_s)
    resolved_device, resolved_compute_type = resolve_device_and_compute_type(
        device=device,
        compute_type=compute_type,
    )
    storage = FilesystemStorage(storage_dir)
    item_id = item_id_for_url(url)
    _log(f"[identify] item_id={item_id}", progress=progress)
    resume_criteria = {
        "source_type": "youtube",
        "source_url": url,
        "provider": LOCAL_PROVIDER,
        "input_adapter": "yt-dlp",
        "profile": profile,
        "model": resolved_model,
        "device": device,
        "compute_type": compute_type,
        "language": language,
        "chunk_duration_s": requested_chunk_duration_s,
        "overlap_s": overlap_s,
        "allow_estimated_subtitles": allow_estimated_subtitles,
    }
    run_state = {
        **resume_criteria,
        "resolved_device": resolved_device,
        "resolved_compute_type": resolved_compute_type,
    }
    run_paths = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    if run_paths is None:
        run_paths = storage.create_run(item_id=item_id)
        _log(f"[run] created {run_paths.run_dir}", progress=progress)
    else:
        _log(f"[run] resuming {run_paths.run_dir}", progress=progress)
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {**run_state, "status": "running"},
    )

    media = media or FfmpegMedia()
    media_dir = run_paths.run_dir / "media"
    source_info_path = media_dir / "source-info.json"
    existing_source = _existing_youtube_source(media_dir)
    if existing_source is not None:
        _log(f"[youtube] using existing downloaded audio {existing_source}", progress=progress)
        source_info = _load_json(source_info_path) if source_info_path.exists() else {}
        download = YoutubeDownloadResult(
            audio_path=existing_source,
            metadata=source_info.get("metadata", source_info),
        )
    else:
        _log("[youtube] downloading audio with yt-dlp", progress=progress)
        download = youtube_downloader.download_audio(
            url,
            media_dir,
            progress_callback=_progress_callback(progress),
        )
        _write_json_atomic(
            source_info_path,
            {
                "audio_path": str(download.audio_path),
                "metadata": download.metadata,
            },
        )

    source_audio = download.audio_path
    source_hash = sha256_file(source_audio)[:12]
    prepared_audio = media_dir / "prepared.wav"
    if prepared_audio.exists():
        _log(f"[media] using existing prepared audio {prepared_audio}", progress=progress)
    else:
        _log("[media] extracting audio", progress=progress)
        media.extract_audio(
            source_audio,
            prepared_audio,
            progress_callback=_progress_callback(progress),
        )
    duration = media.get_duration(prepared_audio)
    _log(f"[media] duration {duration:.3f}s", progress=progress)
    resolved_chunk_duration_s = resolve_chunk_duration_s(
        requested_chunk_duration_s,
        duration_s=duration,
        device=resolved_device,
    )
    chunking_mode = describe_chunking_mode(
        requested_chunk_duration_s,
        resolved_chunk_duration_s=resolved_chunk_duration_s,
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **run_state,
            "duration_s": duration,
            "downloaded_audio_path": str(source_audio),
            "downloaded_audio_sha256_12": source_hash,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "running",
        },
    )

    _log(f"[provider] loading faster-whisper model={resolved_model}", progress=progress)
    provider_factory = local_provider_factory or FasterWhisperProvider
    local_provider = provider_factory(
        model=resolved_model,
        device=resolved_device,
        compute_type=resolved_compute_type,
    )
    _log(
        "[provider] ready device="
        f"{local_provider.device} compute_type={local_provider.compute_type}",
        progress=progress,
    )
    transcript = _run_local_transcription(
        local_provider,
        media,
        run_paths,
        prepared_audio,
        source=url,
        duration=duration,
        language=language,
        resolved_chunk_duration_s=resolved_chunk_duration_s,
        overlap_s=overlap_s,
        resume=resume,
        progress=progress,
    )

    _finalize_run(
        storage,
        run_paths,
        transcript=transcript,
        allow_estimated_subtitles=allow_estimated_subtitles,
        progress=progress,
        metadata={
            "source_type": "youtube",
            "source_url": url,
            "provider": LOCAL_PROVIDER,
            "transcription_provider": "faster-whisper",
            "input_adapter": "yt-dlp",
            "profile": profile,
            "model": resolved_model,
            "device": local_provider.device,
            "compute_type": local_provider.compute_type,
            "requested_device": device,
            "requested_compute_type": compute_type,
            **_language_metadata(requested_language=language, transcript=transcript),
            "allow_estimated_subtitles": allow_estimated_subtitles,
            "chunk_duration_s": requested_chunk_duration_s,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "overlap_s": overlap_s,
            "downloaded_audio_path": str(source_audio),
            "downloaded_audio_size_bytes": source_audio.stat().st_size,
            "downloaded_audio_sha256_12": source_hash,
            **_youtube_metadata_fields(download.metadata),
            "resumed": resume,
        },
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **run_state,
            "duration_s": duration,
            "downloaded_audio_path": str(source_audio),
            "downloaded_audio_sha256_12": source_hash,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "completed",
        },
    )
    _log(f"[done] {run_paths.run_dir}", progress=progress)
    return run_paths.run_dir


def _transcribe_youtube_groq(
    url: str,
    *,
    storage_dir: Path,
    model: str | None,
    language: str | None,
    allow_estimated_subtitles: bool,
    chunk_duration_s: ChunkDurationSetting,
    overlap_s: float,
    resume: bool,
    progress: bool,
    diarize: bool,
    num_speakers: int | None,
    provider_timeout_s: float,
    youtube_downloader: YtDlpYoutubeDownloader | None,
    media: FfmpegMedia | None,
    groq_provider_factory: Callable[..., GroqProvider] | None,
) -> Path:
    if diarize or num_speakers is not None:
        raise ValueError("Groq does not support diarization")
    if youtube_downloader is None:
        ensure_yt_dlp_installed()
        youtube_downloader = YtDlpYoutubeDownloader()

    resolved_model = model or GROQ_DEFAULT_MODEL
    requested_chunk_duration_s = normalize_chunk_duration_setting(chunk_duration_s)
    storage = FilesystemStorage(storage_dir)
    item_id = item_id_for_url(url)
    _log(f"[identify] item_id={item_id}", progress=progress)
    resume_criteria = {
        "source_type": "youtube",
        "source_url": url,
        "provider": GROQ_PROVIDER,
        "input_adapter": "yt-dlp",
        "model": resolved_model,
        "language": language,
        "chunk_duration_s": requested_chunk_duration_s,
        "overlap_s": overlap_s,
        "allow_estimated_subtitles": allow_estimated_subtitles,
    }
    run_paths = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    if run_paths is None:
        run_paths = storage.create_run(item_id=item_id)
        _log(f"[run] created {run_paths.run_dir}", progress=progress)
    else:
        _log(f"[run] resuming {run_paths.run_dir}", progress=progress)
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {**resume_criteria, "status": "running"},
    )

    media = media or FfmpegMedia()
    media_dir = run_paths.run_dir / "media"
    source_info_path = media_dir / "source-info.json"
    existing_source = _existing_youtube_source(media_dir)
    if existing_source is not None:
        _log(f"[youtube] using existing downloaded audio {existing_source}", progress=progress)
        source_info = _load_json(source_info_path) if source_info_path.exists() else {}
        download = YoutubeDownloadResult(
            audio_path=existing_source,
            metadata=source_info.get("metadata", source_info),
        )
    else:
        _log("[youtube] downloading audio with yt-dlp", progress=progress)
        download = youtube_downloader.download_audio(
            url,
            media_dir,
            progress_callback=_progress_callback(progress),
        )
        _write_json_atomic(
            source_info_path,
            {
                "audio_path": str(download.audio_path),
                "metadata": download.metadata,
            },
        )

    source_audio = download.audio_path
    source_hash = sha256_file(source_audio)[:12]
    duration = media.get_duration(source_audio)
    remote_input_size = source_audio.stat().st_size
    _log(
        f"[media] remote input {remote_input_size / (1024 * 1024):.1f} MB "
        f"duration {duration:.3f}s",
        progress=progress,
    )
    resolved_chunk_duration_s = resolve_remote_chunk_duration_s(
        requested_chunk_duration_s,
        duration_s=duration,
        remote_input_size_bytes=remote_input_size,
        file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
        auto_chunk_duration_s=AUTO_GROQ_CHUNK_DURATION_S,
        provider_name="Groq",
    )
    chunking_mode = describe_remote_chunking_mode(
        requested_chunk_duration_s,
        remote_input_size_bytes=remote_input_size,
        resolved_chunk_duration_s=resolved_chunk_duration_s,
        file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **resume_criteria,
            "duration_s": duration,
            "downloaded_audio_path": str(source_audio),
            "downloaded_audio_sha256_12": source_hash,
            "remote_input_size_bytes": remote_input_size,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "running",
        },
    )

    _log(f"[provider] loading groq model={resolved_model}", progress=progress)
    provider_factory = groq_provider_factory or GroqProvider
    remote_provider = provider_factory(model=resolved_model, timeout_s=provider_timeout_s)
    _log("[provider] ready provider=groq mode=file", progress=progress)
    try:
        transcript = _run_groq_file_transcription(
            remote_provider,
            media,
            run_paths,
            source_audio,
            original_source=url,
            duration=duration,
            language=language,
            resolved_chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
        )
    except GroqProviderError as exc:
        if resolved_chunk_duration_s is not None or exc.kind not in {"content_too_large", "timeout", "transient"}:
            raise
        fallback_chunk_duration = resolve_remote_chunk_duration_s(
            AUTO_CHUNK,
            duration_s=duration,
            remote_input_size_bytes=GROQ_FILE_LIMIT_BYTES,
            file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
            auto_chunk_duration_s=AUTO_GROQ_CHUNK_DURATION_S,
            provider_name="Groq",
        )
        _log(
            f"[provider] full upload failed ({exc.kind}); retrying with groq chunks",
            progress=progress,
        )
        transcript = _run_groq_file_transcription(
            remote_provider,
            media,
            run_paths,
            source_audio,
            original_source=url,
            duration=duration,
            language=language,
            resolved_chunk_duration_s=fallback_chunk_duration,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
        )
        chunking_mode = "fallback-error"
        resolved_chunk_duration_s = fallback_chunk_duration

    _finalize_run(
        storage,
        run_paths,
        transcript=transcript,
        allow_estimated_subtitles=allow_estimated_subtitles,
        progress=progress,
        metadata={
            "source_type": "youtube",
            "source_url": url,
            "provider": GROQ_PROVIDER,
            "transcription_provider": GROQ_PROVIDER,
            "input_adapter": "yt-dlp",
            "model": resolved_model,
            **_language_metadata(requested_language=language, transcript=transcript),
            "allow_estimated_subtitles": allow_estimated_subtitles,
            "provider_timeout_s": provider_timeout_s,
            "remote_input_path": str(source_audio),
            "remote_input_size_bytes": remote_input_size,
            "remote_provider_file_limit_bytes": GROQ_FILE_LIMIT_BYTES,
            "remote_chunk_policy": "full-input-first",
            "estimated_cost_usd": estimate_remote_cost_usd(
                duration,
                cost_per_hour_usd=GROQ_COST_PER_HOUR_USD,
            ),
            "chunk_duration_s": requested_chunk_duration_s,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "overlap_s": overlap_s,
            "downloaded_audio_path": str(source_audio),
            "downloaded_audio_size_bytes": source_audio.stat().st_size,
            "downloaded_audio_sha256_12": source_hash,
            **_youtube_metadata_fields(download.metadata),
            "resumed": resume,
        },
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **resume_criteria,
            "duration_s": duration,
            "downloaded_audio_path": str(source_audio),
            "downloaded_audio_sha256_12": source_hash,
            "remote_input_size_bytes": remote_input_size,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "completed",
        },
    )
    _log(f"[done] {run_paths.run_dir}", progress=progress)
    return run_paths.run_dir


def _transcribe_file_elevenlabs(
    path: Path,
    *,
    storage_dir: Path,
    model: str | None,
    language: str | None,
    allow_estimated_subtitles: bool,
    chunk_duration_s: ChunkDurationSetting,
    overlap_s: float,
    resume: bool,
    progress: bool,
    diarize: bool,
    num_speakers: int | None,
    tag_audio_events: bool,
    provider_timeout_s: float,
    remote_audio_format: str,
    remote_audio_bitrate_kbps: int,
) -> Path:
    remote_audio_format = _normalize_remote_audio_format(remote_audio_format)
    resolved_model = model or ELEVENLABS_DEFAULT_MODEL
    requested_chunk_duration_s = normalize_chunk_duration_setting(chunk_duration_s)
    storage = FilesystemStorage(storage_dir)

    source_size = path.stat().st_size
    _log(
        f"[identify] hashing source file for stable item_id ({_format_bytes(source_size)})",
        progress=progress,
    )
    if source_size >= 1024 * 1024 * 1024:
        _log("[identify] large file; this can take a while", progress=progress)
    item_id = item_id_for_file(
        path,
        progress_callback=_HashProgressLogger(source_size, progress=progress),
    )
    _log(f"[identify] item_id={item_id}", progress=progress)

    resume_criteria = {
        "source_path": str(path.resolve()),
        "source_type": "file",
        "provider": ELEVENLABS_PROVIDER,
        "model": resolved_model,
        "language": language,
        "allow_estimated_subtitles": allow_estimated_subtitles,
        "chunk_duration_s": requested_chunk_duration_s,
        "overlap_s": overlap_s,
        "diarize": diarize,
        "num_speakers": num_speakers,
        "tag_audio_events": tag_audio_events,
        "remote_audio_format": remote_audio_format,
        "remote_audio_bitrate_kbps": remote_audio_bitrate_kbps,
    }
    run_paths = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    if run_paths is None:
        run_paths = storage.create_run(item_id=item_id)
        _log(f"[run] created {run_paths.run_dir}", progress=progress)
    else:
        _log(f"[run] resuming {run_paths.run_dir}", progress=progress)
    _write_json_atomic(run_paths.run_dir / "run-state.json", {**resume_criteria, "status": "running"})

    media = FfmpegMedia()
    media_dir = run_paths.run_dir / "media"
    remote_input = media_dir / f"remote-input.{remote_audio_format}"
    if remote_input.exists():
        _log(f"[media] using existing remote audio {remote_input}", progress=progress)
    else:
        _log(
            f"[media] extracting remote audio format={remote_audio_format} "
            f"bitrate={remote_audio_bitrate_kbps}k",
            progress=progress,
        )
        media.extract_remote_audio(
            path,
            remote_input,
            audio_format=remote_audio_format,
            bitrate_kbps=remote_audio_bitrate_kbps,
            progress_callback=_progress_callback(progress),
        )
    duration = media.get_duration(remote_input)
    remote_input_size = remote_input.stat().st_size
    _log(
        f"[media] remote input {remote_input_size / (1024 * 1024):.1f} MB "
        f"duration {duration:.3f}s",
        progress=progress,
    )

    resolved_chunk_duration_s = resolve_remote_chunk_duration_s(
        requested_chunk_duration_s,
        duration_s=duration,
        remote_input_size_bytes=remote_input_size,
    )
    chunking_mode = describe_remote_chunking_mode(
        requested_chunk_duration_s,
        remote_input_size_bytes=remote_input_size,
        resolved_chunk_duration_s=resolved_chunk_duration_s,
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **resume_criteria,
            "duration_s": duration,
            "remote_input_path": str(remote_input),
            "remote_input_size_bytes": remote_input_size,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "running",
        },
    )

    _log(f"[provider] loading elevenlabs model={resolved_model}", progress=progress)
    remote_provider = ElevenLabsProvider(
        model=resolved_model,
        timeout_s=provider_timeout_s,
        tag_audio_events=tag_audio_events,
    )
    _log("[provider] ready provider=elevenlabs mode=file", progress=progress)

    try:
        transcript = _run_elevenlabs_file_transcription(
            remote_provider,
            media,
            run_paths,
            remote_input,
            original_source=path,
            duration=duration,
            language=language,
            diarize=diarize,
            num_speakers=num_speakers,
            resolved_chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
        )
    except ElevenLabsProviderError as exc:
        if resolved_chunk_duration_s is not None or exc.kind not in {"content_too_large", "timeout", "transient"}:
            raise
        fallback_chunk_duration = resolve_remote_chunk_duration_s(
            AUTO_CHUNK,
            duration_s=duration,
            remote_input_size_bytes=ELEVENLABS_FILE_LIMIT_BYTES,
        )
        _log(
            f"[provider] full upload failed ({exc.kind}); retrying with remote chunks",
            progress=progress,
        )
        transcript = _run_elevenlabs_file_transcription(
            remote_provider,
            media,
            run_paths,
            remote_input,
            original_source=path,
            duration=duration,
            language=language,
            diarize=diarize,
            num_speakers=num_speakers,
            resolved_chunk_duration_s=fallback_chunk_duration,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
        )
        chunking_mode = "fallback-error"
        resolved_chunk_duration_s = fallback_chunk_duration

    _finalize_run(
        storage,
        run_paths,
        transcript=transcript,
        allow_estimated_subtitles=allow_estimated_subtitles,
        progress=progress,
        metadata={
            "source_path": str(path.resolve()),
            "source_type": "file",
            "provider": ELEVENLABS_PROVIDER,
            "transcription_provider": ELEVENLABS_PROVIDER,
            "model": resolved_model,
            **_language_metadata(requested_language=language, transcript=transcript),
            "allow_estimated_subtitles": allow_estimated_subtitles,
            "diarize": diarize,
            "num_speakers": num_speakers,
            "tag_audio_events": tag_audio_events,
            "provider_timeout_s": provider_timeout_s,
            "remote_audio_format": remote_audio_format,
            "remote_audio_bitrate_kbps": remote_audio_bitrate_kbps,
            "remote_input_path": str(remote_input),
            "remote_input_size_bytes": remote_input_size,
            "remote_provider_file_limit_bytes": ELEVENLABS_FILE_LIMIT_BYTES,
            "remote_chunk_policy": "full-input-first",
            "estimated_cost_usd": estimate_remote_cost_usd(duration),
            "chunk_duration_s": requested_chunk_duration_s,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "overlap_s": overlap_s,
            "resumed": resume,
        },
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **resume_criteria,
            "duration_s": duration,
            "remote_input_path": str(remote_input),
            "remote_input_size_bytes": remote_input_size,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "completed",
        },
    )
    _log(f"[done] {run_paths.run_dir}", progress=progress)
    return run_paths.run_dir


def _transcribe_file_groq(
    path: Path,
    *,
    storage_dir: Path,
    model: str | None,
    language: str | None,
    allow_estimated_subtitles: bool,
    chunk_duration_s: ChunkDurationSetting,
    overlap_s: float,
    resume: bool,
    progress: bool,
    diarize: bool,
    num_speakers: int | None,
    provider_timeout_s: float,
    remote_audio_format: str,
    remote_audio_bitrate_kbps: int,
) -> Path:
    if diarize or num_speakers is not None:
        raise ValueError("Groq does not support diarization")
    remote_audio_format = _normalize_remote_audio_format(remote_audio_format)
    resolved_model = model or GROQ_DEFAULT_MODEL
    requested_chunk_duration_s = normalize_chunk_duration_setting(chunk_duration_s)
    storage = FilesystemStorage(storage_dir)

    source_size = path.stat().st_size
    _log(
        f"[identify] hashing source file for stable item_id ({_format_bytes(source_size)})",
        progress=progress,
    )
    if source_size >= 1024 * 1024 * 1024:
        _log("[identify] large file; this can take a while", progress=progress)
    item_id = item_id_for_file(
        path,
        progress_callback=_HashProgressLogger(source_size, progress=progress),
    )
    _log(f"[identify] item_id={item_id}", progress=progress)

    resume_criteria = {
        "source_path": str(path.resolve()),
        "source_type": "file",
        "provider": GROQ_PROVIDER,
        "model": resolved_model,
        "language": language,
        "allow_estimated_subtitles": allow_estimated_subtitles,
        "chunk_duration_s": requested_chunk_duration_s,
        "overlap_s": overlap_s,
        "remote_audio_format": remote_audio_format,
        "remote_audio_bitrate_kbps": remote_audio_bitrate_kbps,
    }
    run_paths = find_resumable_run(storage, item_id, resume_criteria) if resume else None
    if run_paths is None:
        run_paths = storage.create_run(item_id=item_id)
        _log(f"[run] created {run_paths.run_dir}", progress=progress)
    else:
        _log(f"[run] resuming {run_paths.run_dir}", progress=progress)
    _write_json_atomic(run_paths.run_dir / "run-state.json", {**resume_criteria, "status": "running"})

    media = FfmpegMedia()
    media_dir = run_paths.run_dir / "media"
    remote_input = media_dir / f"remote-input.{remote_audio_format}"
    if remote_input.exists():
        _log(f"[media] using existing remote audio {remote_input}", progress=progress)
    else:
        _log(
            f"[media] extracting remote audio format={remote_audio_format} "
            f"bitrate={remote_audio_bitrate_kbps}k",
            progress=progress,
        )
        media.extract_remote_audio(
            path,
            remote_input,
            audio_format=remote_audio_format,
            bitrate_kbps=remote_audio_bitrate_kbps,
            progress_callback=_progress_callback(progress),
        )
    duration = media.get_duration(remote_input)
    remote_input_size = remote_input.stat().st_size
    _log(
        f"[media] remote input {remote_input_size / (1024 * 1024):.1f} MB "
        f"duration {duration:.3f}s",
        progress=progress,
    )

    resolved_chunk_duration_s = resolve_remote_chunk_duration_s(
        requested_chunk_duration_s,
        duration_s=duration,
        remote_input_size_bytes=remote_input_size,
        file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
        auto_chunk_duration_s=AUTO_GROQ_CHUNK_DURATION_S,
        provider_name="Groq",
    )
    chunking_mode = describe_remote_chunking_mode(
        requested_chunk_duration_s,
        remote_input_size_bytes=remote_input_size,
        resolved_chunk_duration_s=resolved_chunk_duration_s,
        file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **resume_criteria,
            "duration_s": duration,
            "remote_input_path": str(remote_input),
            "remote_input_size_bytes": remote_input_size,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "running",
        },
    )

    _log(f"[provider] loading groq model={resolved_model}", progress=progress)
    remote_provider = GroqProvider(model=resolved_model, timeout_s=provider_timeout_s)
    _log("[provider] ready provider=groq mode=file", progress=progress)
    try:
        transcript = _run_groq_file_transcription(
            remote_provider,
            media,
            run_paths,
            remote_input,
            original_source=str(path),
            duration=duration,
            language=language,
            resolved_chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
        )
    except GroqProviderError as exc:
        if resolved_chunk_duration_s is not None or exc.kind not in {"content_too_large", "timeout", "transient"}:
            raise
        fallback_chunk_duration = resolve_remote_chunk_duration_s(
            AUTO_CHUNK,
            duration_s=duration,
            remote_input_size_bytes=GROQ_FILE_LIMIT_BYTES,
            file_limit_bytes=GROQ_FILE_LIMIT_BYTES,
            auto_chunk_duration_s=AUTO_GROQ_CHUNK_DURATION_S,
            provider_name="Groq",
        )
        _log(
            f"[provider] full upload failed ({exc.kind}); retrying with groq chunks",
            progress=progress,
        )
        transcript = _run_groq_file_transcription(
            remote_provider,
            media,
            run_paths,
            remote_input,
            original_source=str(path),
            duration=duration,
            language=language,
            resolved_chunk_duration_s=fallback_chunk_duration,
            overlap_s=overlap_s,
            resume=resume,
            progress=progress,
        )
        chunking_mode = "fallback-error"
        resolved_chunk_duration_s = fallback_chunk_duration

    _finalize_run(
        storage,
        run_paths,
        transcript=transcript,
        allow_estimated_subtitles=allow_estimated_subtitles,
        progress=progress,
        metadata={
            "source_path": str(path.resolve()),
            "source_type": "file",
            "provider": GROQ_PROVIDER,
            "transcription_provider": GROQ_PROVIDER,
            "model": resolved_model,
            **_language_metadata(requested_language=language, transcript=transcript),
            "allow_estimated_subtitles": allow_estimated_subtitles,
            "provider_timeout_s": provider_timeout_s,
            "remote_audio_format": remote_audio_format,
            "remote_audio_bitrate_kbps": remote_audio_bitrate_kbps,
            "remote_input_path": str(remote_input),
            "remote_input_size_bytes": remote_input_size,
            "remote_provider_file_limit_bytes": GROQ_FILE_LIMIT_BYTES,
            "remote_chunk_policy": "full-input-first",
            "estimated_cost_usd": estimate_remote_cost_usd(
                duration,
                cost_per_hour_usd=GROQ_COST_PER_HOUR_USD,
            ),
            "chunk_duration_s": requested_chunk_duration_s,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "overlap_s": overlap_s,
            "resumed": resume,
        },
    )
    _write_json_atomic(
        run_paths.run_dir / "run-state.json",
        {
            **resume_criteria,
            "duration_s": duration,
            "remote_input_path": str(remote_input),
            "remote_input_size_bytes": remote_input_size,
            "resolved_chunk_duration_s": resolved_chunk_duration_s,
            "chunking_mode": chunking_mode,
            "status": "completed",
        },
    )
    _log(f"[done] {run_paths.run_dir}", progress=progress)
    return run_paths.run_dir


def _run_elevenlabs_file_transcription(
    provider: ElevenLabsProvider,
    media: FfmpegMedia,
    run_paths: RunPaths,
    remote_input: Path,
    *,
    original_source: Path,
    duration: float,
    language: str | None,
    diarize: bool,
    num_speakers: int | None,
    resolved_chunk_duration_s: float | None,
    overlap_s: float,
    resume: bool,
    progress: bool,
) -> CanonicalTranscript:
    if resolved_chunk_duration_s is not None and duration > resolved_chunk_duration_s:
        _log(
            f"[chunking] remote fallback duration={resolved_chunk_duration_s:.0f}s",
            progress=progress,
        )
        chunks = _load_or_create_chunks(
            media,
            remote_input,
            run_paths.run_dir / "media" / "chunks",
            duration=duration,
            chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            progress=progress,
        )
        partial_dir = run_paths.run_dir / "partials"
        partial_dir.mkdir(parents=True, exist_ok=True)
        partials = []
        for position, chunk in enumerate(chunks, start=1):
            if chunk.path is None:
                raise RuntimeError("chunk path missing")
            partial_path = partial_dir / f"chunk_{chunk.index:04d}.canonical.json"
            if resume and partial_path.exists():
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} loading partial",
                    progress=progress,
                )
                partial = _load_transcript(partial_path)
            else:
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} started",
                    progress=progress,
                )
                partial = provider.transcribe_file(
                    chunk.path,
                    language=language,
                    diarize=diarize,
                    num_speakers=num_speakers,
                    progress_callback=_progress_callback(progress),
                    progress_label=f"[transcribe] chunk {position}/{len(chunks)}",
                )
                _write_json_atomic(partial_path, partial.to_dict())
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} saved partial",
                    progress=progress,
                )
            partials.append((partial, chunk))
        _log("[merge] merging chunks", progress=progress)
        return merge_chunk_transcripts(
            partials,
            source=str(original_source),
            provider=ELEVENLABS_PROVIDER,
            model=provider.model_name,
            language=language or partials[0][0].language,
            duration=duration,
        )

    _log("[transcribe] remote full file started", progress=progress)
    transcript = provider.transcribe_file(
        remote_input,
        language=language,
        diarize=diarize,
        num_speakers=num_speakers,
        progress_callback=_progress_callback(progress),
        progress_label="[transcribe] remote full file",
    )
    _log("[transcribe] remote full file finished", progress=progress)
    return _retarget_transcript(transcript, source=str(original_source), duration=duration)


def _run_groq_file_transcription(
    provider: GroqProvider,
    media: FfmpegMedia,
    run_paths: RunPaths,
    remote_input: Path,
    *,
    original_source: str,
    duration: float,
    language: str | None,
    resolved_chunk_duration_s: float | None,
    overlap_s: float,
    resume: bool,
    progress: bool,
) -> CanonicalTranscript:
    if resolved_chunk_duration_s is not None and duration > resolved_chunk_duration_s:
        _log(
            f"[chunking] groq remote duration={resolved_chunk_duration_s:.0f}s",
            progress=progress,
        )
        chunks = _load_or_create_chunks(
            media,
            remote_input,
            run_paths.run_dir / "media" / "chunks",
            duration=duration,
            chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            progress=progress,
        )
        partial_dir = run_paths.run_dir / "partials"
        partial_dir.mkdir(parents=True, exist_ok=True)
        partials = []
        for position, chunk in enumerate(chunks, start=1):
            if chunk.path is None:
                raise RuntimeError("chunk path missing")
            partial_path = partial_dir / f"chunk_{chunk.index:04d}.canonical.json"
            if resume and partial_path.exists():
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} loading partial",
                    progress=progress,
                )
                partial = _load_transcript(partial_path)
            else:
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} started",
                    progress=progress,
                )
                partial = provider.transcribe_file(
                    chunk.path,
                    language=language,
                    progress_callback=_progress_callback(progress),
                    progress_label=f"[transcribe] chunk {position}/{len(chunks)}",
                )
                _write_json_atomic(partial_path, partial.to_dict())
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} saved partial",
                    progress=progress,
                )
            partials.append((partial, chunk))
        _log("[merge] merging chunks", progress=progress)
        return merge_chunk_transcripts(
            partials,
            source=original_source,
            provider=GROQ_PROVIDER,
            model=provider.model_name,
            language=language or partials[0][0].language,
            duration=duration,
        )

    _log("[transcribe] groq full file started", progress=progress)
    transcript = provider.transcribe_file(
        remote_input,
        language=language,
        progress_callback=_progress_callback(progress),
        progress_label="[transcribe] groq full file",
    )
    _log("[transcribe] groq full file finished", progress=progress)
    return _retarget_transcript(transcript, source=original_source, duration=duration)


def _run_local_transcription(
    provider: FasterWhisperProvider,
    media: FfmpegMedia,
    run_paths: RunPaths,
    prepared_audio: Path,
    *,
    source: str,
    duration: float,
    language: str | None,
    resolved_chunk_duration_s: float | None,
    overlap_s: float,
    resume: bool,
    progress: bool,
) -> CanonicalTranscript:
    if resolved_chunk_duration_s is not None and duration > resolved_chunk_duration_s:
        chunks = _load_or_create_chunks(
            media,
            prepared_audio,
            run_paths.run_dir / "media" / "chunks",
            duration=duration,
            chunk_duration_s=resolved_chunk_duration_s,
            overlap_s=overlap_s,
            progress=progress,
        )
        partial_dir = run_paths.run_dir / "partials"
        partial_dir.mkdir(parents=True, exist_ok=True)
        partials = []
        for position, chunk in enumerate(chunks, start=1):
            if chunk.path is None:
                raise RuntimeError("chunk path missing")
            partial_path = partial_dir / f"chunk_{chunk.index:04d}.canonical.json"
            if resume and partial_path.exists():
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} loading partial",
                    progress=progress,
                )
                partial = _load_transcript(partial_path)
            else:
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} started",
                    progress=progress,
                )
                partial = provider.transcribe(chunk.path, language=language)
                _write_json_atomic(partial_path, partial.to_dict())
                _log(
                    f"[transcribe] chunk {position}/{len(chunks)} saved partial",
                    progress=progress,
                )
            partials.append((partial, chunk))
        _log("[merge] merging chunks", progress=progress)
        return merge_chunk_transcripts(
            partials,
            source=source,
            provider="faster-whisper",
            model=provider.model_name,
            language=language or partials[0][0].language,
            duration=duration,
        )

    _log("[transcribe] full file started", progress=progress)
    transcript = provider.transcribe(prepared_audio, language=language)
    _log("[transcribe] full file finished", progress=progress)
    return _retarget_transcript(transcript, source=source, duration=duration)


def _finalize_run(
    storage: FilesystemStorage,
    run_paths: RunPaths,
    *,
    transcript: CanonicalTranscript,
    allow_estimated_subtitles: bool,
    progress: bool,
    metadata: dict[str, Any],
) -> None:
    config = SubtitleConfig()
    _log("[subtitles] building SRT/VTT cues", progress=progress)
    cues = SubtitleBuilder(
        config,
        allow_estimated_subtitles=allow_estimated_subtitles,
    ).build(transcript)
    _log("[quality] evaluating artifacts", progress=progress)
    quality = evaluate_quality(
        transcript,
        cues,
        config=config,
        allow_estimated_subtitles=allow_estimated_subtitles,
    )
    _log("[outputs] writing final run artifacts", progress=progress)
    storage.save_run(
        run_paths,
        transcript=transcript,
        cues=cues,
        quality=quality,
        metadata=metadata,
    )


def _retarget_transcript(
    transcript: CanonicalTranscript,
    *,
    source: str,
    duration: float,
) -> CanonicalTranscript:
    return replace(transcript, source=source, duration=duration or transcript.duration)


def _language_metadata(
    *,
    requested_language: str | None,
    transcript: CanonicalTranscript,
) -> dict[str, str | None]:
    return {
        "language": transcript.language,
        "requested_language": requested_language,
        "detected_language": transcript.language,
    }


def _existing_youtube_source(media_dir: Path) -> Path | None:
    if not media_dir.exists():
        return None
    unfinished_suffixes = {".part", ".tmp", ".ytdl"}
    candidates = sorted(
        (
            path
            for path in media_dir.glob("source.*")
            if path.is_file() and path.suffix not in unfinished_suffixes
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _youtube_metadata_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "id": "youtube_video_id",
        "title": "youtube_title",
        "duration": "youtube_duration_s",
        "channel": "youtube_channel",
        "channel_id": "youtube_channel_id",
        "uploader": "youtube_uploader",
        "uploader_id": "youtube_uploader_id",
        "webpage_url": "youtube_webpage_url",
        "extractor": "youtube_extractor",
        "extractor_key": "youtube_extractor_key",
        "upload_date": "youtube_upload_date",
    }
    return {
        output_key: metadata[input_key]
        for input_key, output_key in mapping.items()
        if metadata.get(input_key) is not None
    }


def estimate_remote_cost_usd(
    duration_s: float,
    *,
    cost_per_hour_usd: float = ELEVENLABS_COST_PER_HOUR_USD,
) -> float:
    return round((duration_s / 3600.0) * cost_per_hour_usd, 4)


def resolve_model(*, model: str | None, profile: str) -> str:
    if model:
        return model
    try:
        return MODEL_PROFILES[profile]
    except KeyError as exc:
        choices = ", ".join(sorted(MODEL_PROFILES))
        raise ValueError(f"unknown profile {profile!r}; expected one of: {choices}") from exc


def normalize_chunk_duration_setting(value: ChunkDurationSetting) -> float | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", AUTO_CHUNK}:
            return AUTO_CHUNK
        if normalized in {"off", "none", "false", "disabled", "no"}:
            return None
        try:
            value = float(normalized)
        except ValueError as exc:
            raise ValueError(
                "chunk duration must be seconds, 'auto', or 'off'"
            ) from exc
    value = float(value)
    if value <= 0:
        return None
    return value


def resolve_chunk_duration_s(
    requested: float | str | None,
    *,
    duration_s: float,
    device: str,
) -> float | None:
    requested = normalize_chunk_duration_setting(requested)
    if requested is None:
        return None
    if requested != AUTO_CHUNK:
        return float(requested)
    if duration_s <= AUTO_CHUNK_THRESHOLD_S:
        return None
    return (
        float(AUTO_CUDA_CHUNK_DURATION_S)
        if device == "cuda"
        else float(AUTO_CPU_CHUNK_DURATION_S)
    )


def describe_chunking_mode(
    requested: float | str | None,
    *,
    resolved_chunk_duration_s: float | None,
) -> str:
    requested = normalize_chunk_duration_setting(requested)
    if resolved_chunk_duration_s is None:
        return "disabled" if requested is None else "auto-disabled"
    if requested == AUTO_CHUNK:
        return "auto"
    return "manual"


def resolve_remote_chunk_duration_s(
    requested: float | str | None,
    *,
    duration_s: float,
    remote_input_size_bytes: int,
    file_limit_bytes: int = ELEVENLABS_FILE_LIMIT_BYTES,
    auto_chunk_duration_s: float = AUTO_REMOTE_CHUNK_DURATION_S,
    provider_name: str = "remote provider",
) -> float | None:
    requested = normalize_chunk_duration_setting(requested)
    must_chunk = remote_input_size_bytes >= file_limit_bytes
    if requested is None:
        if must_chunk:
            raise ValueError(
                f"remote input exceeds {provider_name} file limit and chunking is disabled"
            )
        return None
    if requested == AUTO_CHUNK:
        return float(auto_chunk_duration_s) if must_chunk else None
    if must_chunk:
        return min(float(requested), float(auto_chunk_duration_s))
    if duration_s <= float(requested):
        return None
    return float(requested)


def describe_remote_chunking_mode(
    requested: float | str | None,
    *,
    remote_input_size_bytes: int,
    resolved_chunk_duration_s: float | None,
    file_limit_bytes: int = ELEVENLABS_FILE_LIMIT_BYTES,
) -> str:
    requested = normalize_chunk_duration_setting(requested)
    if resolved_chunk_duration_s is None:
        return "remote-full"
    if remote_input_size_bytes >= file_limit_bytes:
        return "remote-fallback-size"
    if requested == AUTO_CHUNK:
        return "remote-full"
    return "manual"


def _normalize_remote_audio_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in REMOTE_AUDIO_FORMATS:
        choices = ", ".join(sorted(REMOTE_AUDIO_FORMATS))
        raise ValueError(f"remote audio format must be one of: {choices}")
    return normalized


def build_resume_criteria(
    *,
    path: Path,
    provider: str = LOCAL_PROVIDER,
    profile: str,
    model: str,
    device: str,
    compute_type: str,
    language: str | None,
    chunk_duration_s: float | str | None,
    overlap_s: float,
    allow_estimated_subtitles: bool,
) -> dict[str, Any]:
    return {
        "source_path": str(Path(path).resolve()),
        "provider": provider,
        "profile": profile,
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "language": language,
        "chunk_duration_s": chunk_duration_s,
        "overlap_s": overlap_s,
        "allow_estimated_subtitles": allow_estimated_subtitles,
    }


def find_resumable_run(
    storage: FilesystemStorage,
    item_id: str,
    criteria: dict[str, Any],
) -> RunPaths | None:
    return _find_resumable_run(storage, item_id, criteria)


def _load_or_create_chunks(
    media: FfmpegMedia,
    prepared_audio: Path,
    chunk_dir: Path,
    *,
    duration: float,
    chunk_duration_s: float,
    overlap_s: float,
    progress: bool,
) -> list[ChunkInfo]:
    planned = plan_chunks(
        duration_s=duration,
        chunk_duration_s=chunk_duration_s,
        overlap_s=overlap_s,
    )
    existing = [
        ChunkInfo(
            index=chunk.index,
            nominal_start=chunk.nominal_start,
            nominal_end=chunk.nominal_end,
            actual_start=chunk.actual_start,
            overlap_left=chunk.overlap_left,
            path=chunk_dir / f"chunk_{chunk.index:04d}.wav",
        )
        for chunk in planned
    ]
    if existing and all(chunk.path is not None and chunk.path.exists() for chunk in existing):
        _log(f"[chunking] using existing {len(existing)} chunks", progress=progress)
        return existing

    _log(f"[chunking] creating {len(planned)} chunks", progress=progress)
    return media.split_audio(
        prepared_audio,
        chunk_dir,
        chunk_duration_s=chunk_duration_s,
        overlap_s=overlap_s,
        progress_callback=_progress_callback(progress),
    )


def _find_resumable_run(
    storage: FilesystemStorage,
    item_id: str,
    criteria: dict[str, Any],
) -> RunPaths | None:
    item_dir = storage.root / "items" / item_id
    runs_dir = item_dir / "runs"
    if not runs_dir.exists():
        return None
    for run_dir in sorted(
        (path for path in runs_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        if (run_dir / "run.json").exists():
            continue
        state_path = run_dir / "run-state.json"
        if not state_path.exists():
            continue
        state = _load_json(state_path)
        if any(_state_value(state, key) != value for key, value in criteria.items()):
            continue
        return RunPaths(
            item_id=item_id,
            run_id=run_dir.name,
            item_dir=item_dir,
            run_dir=run_dir,
            latest_path=item_dir / "latest.json",
        )
    return None


def _state_value(state: dict[str, Any], key: str) -> Any:
    value = state.get(key)
    if key == "provider" and value is None:
        return LOCAL_PROVIDER
    return value


def _load_json(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _load_transcript(path: Path) -> CanonicalTranscript:
    import json

    return CanonicalTranscript.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _log(message: str, *, progress: bool) -> None:
    if progress:
        print(message, flush=True)


def _progress_callback(progress: bool):
    if not progress:
        return None
    return lambda message: _log(message, progress=True)


class _HashProgressLogger:
    def __init__(
        self,
        total_bytes: int,
        *,
        progress: bool,
        min_interval_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.total_bytes = total_bytes
        self.progress = progress
        self.min_interval_bytes = min_interval_bytes
        self._next_report = min_interval_bytes

    def __call__(self, bytes_read: int) -> None:
        if not self.progress or self.total_bytes <= 0:
            return
        if bytes_read < self._next_report and bytes_read < self.total_bytes:
            return
        pct = min(100.0, bytes_read * 100 / self.total_bytes)
        _log(
            f"[identify] hashed {_format_bytes(bytes_read)} / "
            f"{_format_bytes(self.total_bytes)} ({pct:.1f}%)",
            progress=True,
        )
        while self._next_report <= bytes_read:
            self._next_report += self.min_interval_bytes


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")
