"""MCP tool definitions.

Each docstring here is read by the LLM agent to decide when to invoke the tool.
Treat it as UX copy, not as a code comment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from transcription_mcp.config import Config
from transcription_mcp.jobs import (
    cancel_transcription_job,
    get_transcription_job_artifact,
    get_transcription_job_result,
    get_transcription_job_status,
    start_transcription_job,
)
from transcription_mcp.pipeline import (
    transcribe_file_sync,
    transcribe_media_url_sync,
    transcribe_youtube_sync,
)


def register_tools(mcp: FastMCP, config: Config) -> None:
    @mcp.tool()
    def transcribe_youtube(
        url: Annotated[
            str,
            Field(description="Full YouTube URL to transcribe."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
        ] = None,
        provider_order: Annotated[
            str | None,
            Field(
                description=(
                    "Optional comma-separated providers. Default: groq,elevenlabs,subtitles. "
                    "Use local only when faster-whisper is installed and local CPU/GPU work is desired."
                )
            ),
        ] = None,
        diarize: Annotated[
            bool,
            Field(description="Request speaker diarization. Currently supported by ElevenLabs only."),
        ] = False,
        num_speakers: Annotated[
            int | None,
            Field(description="Optional expected number of speakers for diarization."),
        ] = None,
    ) -> dict[str, Any]:
        """Synchronously transcribe a YouTube video.

        Use for short videos or when a blocking call is acceptable. For long
        videos, prefer `start_youtube_transcription` plus status polling.
        """
        return transcribe_youtube_sync(
            url=url,
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            ytdlp_cookies_file=config.ytdlp_cookies_file,
            ytdlp_proxy=config.ytdlp_proxy,
            cache_ttl_hours=config.cache_ttl_hours,
        )

    @mcp.tool()
    def transcribe_media_url(
        url: Annotated[
            str,
            Field(description="Public media URL supported by yt-dlp or a direct provider source_url."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
        ] = None,
        provider_order: Annotated[
            str | None,
            Field(description="Optional comma-separated providers. Default: groq,elevenlabs."),
        ] = None,
        diarize: Annotated[
            bool,
            Field(description="Request speaker diarization. Currently supported by ElevenLabs only."),
        ] = False,
        num_speakers: Annotated[
            int | None,
            Field(description="Optional expected number of speakers for diarization."),
        ] = None,
    ) -> dict[str, Any]:
        """Synchronously transcribe a public media URL.

        This is not the YouTube captions fallback path; it uses audio providers
        only. Prefer the async tool for long URLs.
        """
        return transcribe_media_url_sync(
            url=url,
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            ytdlp_cookies_file=config.ytdlp_cookies_file,
            ytdlp_proxy=config.ytdlp_proxy,
            cache_ttl_hours=config.cache_ttl_hours,
        )

    @mcp.tool()
    def transcribe_file(
        file_path: Annotated[
            str,
            Field(description="Absolute path to a local audio or video file visible to the MCP host."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
        ] = None,
        provider_order: Annotated[
            str | None,
            Field(description="Optional comma-separated providers. Default: groq,elevenlabs."),
        ] = None,
        diarize: Annotated[
            bool,
            Field(description="Request speaker diarization. Currently supported by ElevenLabs only."),
        ] = False,
        num_speakers: Annotated[
            int | None,
            Field(description="Optional expected number of speakers for diarization."),
        ] = None,
    ) -> dict[str, Any]:
        """Synchronously transcribe a local file visible to the MCP host."""
        return transcribe_file_sync(
            file_path=Path(file_path),
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            cache_ttl_hours=config.cache_ttl_hours,
        )

    @mcp.tool()
    def start_youtube_transcription(
        url: Annotated[
            str,
            Field(description="Full YouTube URL to transcribe asynchronously."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
        ] = None,
        provider_order: Annotated[
            str | None,
            Field(description="Optional comma-separated providers. Default: groq,elevenlabs,subtitles."),
        ] = None,
        diarize: Annotated[
            bool,
            Field(description="Request speaker diarization. Currently supported by ElevenLabs only."),
        ] = False,
        num_speakers: Annotated[
            int | None,
            Field(description="Optional expected number of speakers for diarization."),
        ] = None,
    ) -> dict[str, Any]:
        """Start a background YouTube transcription job and return a run_id.

        Agent workflow: show user_visible_message, keep run_id, and follow
        recommended_next_tool plus recommended_poll_seconds from the response.
        """
        return start_transcription_job(
            source=url,
            source_type="youtube",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            ytdlp_cookies_file=config.ytdlp_cookies_file,
            ytdlp_proxy=config.ytdlp_proxy,
            cache_ttl_hours=config.cache_ttl_hours,
            max_concurrent_jobs=config.max_concurrent_jobs,
            job_ttl_hours=config.job_ttl_hours,
        )

    @mcp.tool()
    def start_media_url_transcription(
        url: Annotated[
            str,
            Field(description="Public media URL supported by yt-dlp or direct provider fetch."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
        ] = None,
        provider_order: Annotated[
            str | None,
            Field(description="Optional comma-separated providers. Default: groq,elevenlabs."),
        ] = None,
        diarize: Annotated[
            bool,
            Field(description="Request speaker diarization. Currently supported by ElevenLabs only."),
        ] = False,
        num_speakers: Annotated[
            int | None,
            Field(description="Optional expected number of speakers for diarization."),
        ] = None,
    ) -> dict[str, Any]:
        """Start a background media URL transcription job and return a run_id.

        Agent workflow: show user_visible_message, keep run_id, and follow
        recommended_next_tool plus recommended_poll_seconds from the response.
        """
        return start_transcription_job(
            source=url,
            source_type="media_url",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            ytdlp_cookies_file=config.ytdlp_cookies_file,
            ytdlp_proxy=config.ytdlp_proxy,
            cache_ttl_hours=config.cache_ttl_hours,
            max_concurrent_jobs=config.max_concurrent_jobs,
            job_ttl_hours=config.job_ttl_hours,
        )

    @mcp.tool()
    def start_file_transcription(
        file_path: Annotated[
            str,
            Field(description="Absolute path to a local audio or video file visible to the MCP host."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
        ] = None,
        provider_order: Annotated[
            str | None,
            Field(description="Optional comma-separated providers. Default: groq,elevenlabs."),
        ] = None,
        diarize: Annotated[
            bool,
            Field(description="Request speaker diarization. Currently supported by ElevenLabs only."),
        ] = False,
        num_speakers: Annotated[
            int | None,
            Field(description="Optional expected number of speakers for diarization."),
        ] = None,
    ) -> dict[str, Any]:
        """Start a background local file transcription job and return a run_id.

        Agent workflow: show user_visible_message, keep run_id, and follow
        recommended_next_tool plus recommended_poll_seconds from the response.
        """
        return start_transcription_job(
            source=file_path,
            source_type="file",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            cache_ttl_hours=config.cache_ttl_hours,
            max_concurrent_jobs=config.max_concurrent_jobs,
            job_ttl_hours=config.job_ttl_hours,
        )

    @mcp.tool()
    def get_transcription_status(
        run_id: Annotated[
            str,
            Field(description="run_id returned by a start_* transcription tool."),
        ],
    ) -> dict[str, Any]:
        """Return persisted status for a background transcription job.

        If status is queued/running/canceling, report user_visible_message and
        poll again after recommended_poll_seconds. If completed, call
        get_transcription_result. If failed/canceled, explain the terminal state.
        """
        return get_transcription_job_status(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def get_transcription_result(
        run_id: Annotated[
            str,
            Field(description="run_id returned by a start_* transcription tool."),
        ],
    ) -> dict[str, Any]:
        """Return the final transcript/result for a completed background job.

        If result_available is false, follow recommended_next_tool. If completed,
        use result.transcript as the primary answer and fetch artifacts only when
        the user asks for timestamps, subtitles, or audit data.
        """
        return get_transcription_job_result(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def get_transcription_artifact(
        run_id: Annotated[
            str,
            Field(description="run_id returned by a start_* transcription tool."),
        ],
        artifact: Annotated[
            str,
            Field(description="Artifact name from result.artifacts, e.g. subtitles_srt or transcript_timestamps_txt."),
        ],
    ) -> dict[str, Any]:
        """Return text content for a completed job artifact by artifact name.

        Artifact names come from result.artifacts or recommended_artifacts.
        Common values include subtitles_srt, subtitles_vtt, transcript_timestamps_txt,
        and audit_txt.
        """
        return get_transcription_job_artifact(
            run_id=run_id,
            artifact=artifact,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def cancel_transcription(
        run_id: Annotated[
            str,
            Field(description="run_id returned by a start_* transcription tool."),
        ],
    ) -> dict[str, Any]:
        """Best-effort cancellation for a running background transcription job."""
        return cancel_transcription_job(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
        )

    @mcp.prompt(
        name="transcribe_with_progress",
        title="Transcribe with visible progress",
        description="Recommended workflow for async transcription with user-visible updates.",
    )
    def transcribe_with_progress(source: str, source_type: str = "youtube") -> str:
        """Guide an agent through a transcription request with progress updates."""
        return f"""Transcribe this source while keeping the user informed.

Source: {source}
Source type: {source_type}

Workflow:
1. For YouTube URLs, call start_youtube_transcription. For other media URLs, call
   start_media_url_transcription. For local files, call start_file_transcription.
2. Immediately tell the user the job started using user_visible_message and keep run_id.
3. While status is queued, running, or canceling, call get_transcription_status after
   recommended_poll_seconds and report user_visible_message to the user.
4. When status is completed, call get_transcription_result with the same run_id.
5. Use result.transcript as the main answer. Use get_transcription_artifact only if the
   user asks for subtitles, timestamps, audit data, or another listed artifact.
6. If status is failed or canceled, stop polling and explain the terminal state using
   user_visible_message plus error, failed_attempts, and logs when present.
"""
