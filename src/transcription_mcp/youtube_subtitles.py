"""Fetch YouTube transcripts directly from YouTube's captions/CC.

Works from any IP (cloud or residential) because it hits the YouTube
captions API, not the video binary. Use this BEFORE attempting audio
download with yt-dlp — most popular videos have at least
auto-generated captions.

Returns the same shape as the audio path so the agent gets a consistent
response regardless of which method was used.

Uses youtube-transcript-api >= 1.x (instance-based API: `.fetch(...)`).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import YouTubeTranscriptApiException
except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
    raise RuntimeError(
        "youtube-transcript-api is not installed. Add it to dependencies."
    ) from exc


class NoSubtitlesAvailable(RuntimeError):
    """Raised when subtitles cannot be retrieved for any reason."""


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DEFAULT_LANG_PREFERENCE = ("en", "es", "pt", "fr", "de", "it")


def extract_video_id(url: str) -> str:
    """Pull the 11-character video ID out of any YouTube URL variant."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()

    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(candidate):
            return candidate

    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v") or []
            if values and _VIDEO_ID_RE.match(values[0]):
                return values[0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            if _VIDEO_ID_RE.match(parts[1]):
                return parts[1]

    # Last resort: maybe the user passed a bare 11-char ID
    if _VIDEO_ID_RE.match(url.strip()):
        return url.strip()

    raise ValueError(f"could not extract a YouTube video id from URL: {url!r}")


def fetch_subtitles_transcript(
    url: str,
    *,
    language: str | None = None,
) -> dict[str, Any]:
    """Try to fetch captions for the video. Raises NoSubtitlesAvailable if none.

    Strategy:
      - If `language` provided, attempt it first, then any available.
      - If unset, try a common-language preference list, then any available.
    """
    video_id = extract_video_id(url)
    api = YouTubeTranscriptApi()

    if language:
        attempts: tuple[tuple[str, ...], ...] = ((language,), _DEFAULT_LANG_PREFERENCE)
    else:
        attempts = (_DEFAULT_LANG_PREFERENCE,)

    fetched = None
    last_exc: Exception | None = None
    for langs in attempts:
        try:
            fetched = api.fetch(video_id, languages=list(langs))
            break
        except YouTubeTranscriptApiException as exc:
            last_exc = exc
            continue

    # Final fallback: enumerate transcripts and grab any one
    if fetched is None:
        try:
            transcript_list = api.list(video_id)
            transcript = next(iter(transcript_list))
            fetched = transcript.fetch()
        except (YouTubeTranscriptApiException, StopIteration) as exc:
            cause = last_exc or exc
            raise NoSubtitlesAvailable(
                f"no captions available for video {video_id}: "
                f"{type(cause).__name__}: {cause}"
            ) from cause

    snippets = list(fetched)
    if not snippets:
        raise NoSubtitlesAvailable(f"empty caption list for video {video_id}")

    text = " ".join((snippet.text or "").strip() for snippet in snippets if snippet.text)
    last = snippets[-1]
    duration_s = float(last.start) + float(last.duration)
    used_language = getattr(fetched, "language_code", None) or "unknown"

    metadata = _fetch_oembed_metadata(video_id)

    return {
        "transcript": text.strip(),
        "language": used_language,
        "duration_s": round(duration_s, 3),
        "model": "youtube-captions",
        "provider": "youtube-transcript-api",
        "estimated_cost_usd": 0.0,
        "youtube": {
            "video_id": video_id,
            "title": metadata.get("title"),
            "channel": metadata.get("author_name"),
        },
        "method": "subtitles",
        "quality_status": "pass",
        "audit": {
            "status": "info",
            "verdict": "fetched from YouTube captions (no quality scoring performed)",
        },
    }


def _fetch_oembed_metadata(video_id: str) -> dict[str, Any]:
    """Hit YouTube's oEmbed endpoint for title/author. No auth, no API key."""
    url = (
        f"https://www.youtube.com/oembed?"
        f"url=https://www.youtube.com/watch?v={video_id}&format=json"
    )
    try:
        response = httpx.get(url, timeout=10.0)
        if response.status_code == 200:
            return response.json()
    except Exception:  # pragma: no cover - best-effort metadata
        pass
    return {}
