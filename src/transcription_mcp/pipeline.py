"""Three-level YouTube transcription chain.

Tried in order; first success wins. Each failure is captured into
`failed_attempts` so the agent can tell the user what happened.

  1. **Groq + yt-dlp** (cheapest, ~$0.04/hr). Downloads the audio
     ourselves with yt-dlp and sends it to Groq Whisper. Fails when
     YouTube blocks the host IP (common on cloud VPS — DigitalOcean,
     AWS, Hetzner, etc.).

  2. **ElevenLabs Scribe v2 via source_url** (~$0.22/hr).
     ElevenLabs downloads the YouTube URL on THEIR infrastructure, so
     our host IP is irrelevant. Always works from any cloud host as
     long as the video is publicly accessible. Higher quality than
     Groq Whisper turbo.

  3. **YouTube captions / CC** (free, lower quality). Last resort
     when both pay providers fail or auth is missing. Often
     auto-generated, no punctuation, errors in proper nouns.

The response always includes a `method` field telling the agent which
level succeeded. If any earlier level failed, `failed_attempts` lists
the reason for each so the agent can explain to the user.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from transcription_v4.pipeline import transcribe_youtube as v4_transcribe_youtube
from transcription_v4.providers import ELEVENLABS_PROVIDER, GROQ_PROVIDER

from transcription_mcp.youtube_subtitles import (
    NoSubtitlesAvailable,
    fetch_subtitles_transcript,
)


logger = logging.getLogger("transcription_mcp.pipeline")


class TranscriptionFailed(RuntimeError):
    """All three transcription methods were exhausted without producing a result."""


def transcribe_youtube_sync(
    *,
    url: str,
    language: str | None,
    workspace_dir: Path,
) -> dict[str, Any]:
    """Run the 3-level transcription chain. Returns the first success."""
    failed_attempts: dict[str, str] = {}

    # --- LEVEL 1: Groq (cheapest, needs yt-dlp to download)
    try:
        run_dir = v4_transcribe_youtube(
            url,
            storage_dir=workspace_dir / "v4-storage",
            provider=GROQ_PROVIDER,
            language=language,
            # stdout MUST stay clean for stdio MCP transport.
            progress=False,
        )
    except Exception as exc:  # noqa: BLE001 — many concrete types from v4 / yt-dlp
        failed_attempts["groq"] = _describe_exception(exc)
        logger.warning(
            "level 1 (groq+yt-dlp) failed: %s",
            failed_attempts["groq"],
        )
    else:
        result = _read_run_artifacts(run_dir)
        result["method"] = "groq"
        if failed_attempts:
            result["failed_attempts"] = failed_attempts
        logger.info(
            "transcribed via groq: video=%s lang=%s chars=%d cost=%s",
            result.get("youtube", {}).get("video_id"),
            result.get("language"),
            len(result.get("transcript", "")),
            result.get("estimated_cost_usd"),
        )
        return result

    # --- LEVEL 2: ElevenLabs source_url (cloud-friendly, no yt-dlp needed)
    try:
        run_dir = v4_transcribe_youtube(
            url,
            storage_dir=workspace_dir / "v4-storage",
            provider=ELEVENLABS_PROVIDER,
            language=language,
            progress=False,
        )
    except Exception as exc:  # noqa: BLE001
        failed_attempts["elevenlabs"] = _describe_exception(exc)
        logger.warning(
            "level 2 (elevenlabs source_url) failed: %s",
            failed_attempts["elevenlabs"],
        )
    else:
        result = _read_run_artifacts(run_dir)
        result["method"] = "elevenlabs"
        result["failed_attempts"] = failed_attempts
        logger.info(
            "transcribed via elevenlabs: video=%s lang=%s chars=%d cost=%s",
            result.get("youtube", {}).get("video_id"),
            result.get("language"),
            len(result.get("transcript", "")),
            result.get("estimated_cost_usd"),
        )
        return result

    # --- LEVEL 3: YouTube captions (degraded last resort)
    try:
        result = fetch_subtitles_transcript(url, language=language)
    except NoSubtitlesAvailable as exc:
        failed_attempts["subtitles"] = _describe_exception(exc)
        message = (
            "All transcription methods failed.\n"
            + "\n".join(
                f"  - {provider}: {reason}"
                for provider, reason in failed_attempts.items()
            )
            + "\nMost common cause on cloud VPS: YouTube blocked yt-dlp by IP "
            "(level 1) AND ElevenLabs auth/quota issue (level 2). Check "
            "GROQ_API_KEY and ELEVENLABS_API_KEY are valid."
        )
        logger.error(message)
        raise TranscriptionFailed(message) from exc

    result["failed_attempts"] = failed_attempts
    logger.info(
        "transcribed via subtitles fallback: video=%s lang=%s chars=%d",
        result["youtube"]["video_id"],
        result["language"],
        len(result["transcript"]),
    )
    return result


def _describe_exception(exc: BaseException) -> str:
    """Format an exception for the failed_attempts dict.

    Includes the exception type name plus a short message. v4's typed
    errors carry a `kind` attribute (auth, content_too_large, etc.) —
    surface it when present so the agent can react accordingly.
    """
    kind = getattr(exc, "kind", None)
    if kind:
        return f"{type(exc).__name__}[{kind}]: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _read_run_artifacts(run_dir: Path) -> dict[str, Any]:
    transcript_text = (run_dir / "transcript.txt").read_text(encoding="utf-8").strip()
    canonical = json.loads((run_dir / "canonical.json").read_text(encoding="utf-8"))
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    quality = json.loads((run_dir / "quality.json").read_text(encoding="utf-8"))

    metadata = run_record.get("metadata", {})
    return {
        "transcript": transcript_text,
        "language": canonical.get("language"),
        "duration_s": canonical.get("duration"),
        "model": canonical.get("model"),
        "provider": canonical.get("provider"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd"),
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
        "run_dir": str(run_dir),
    }
