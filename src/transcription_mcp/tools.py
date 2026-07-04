"""MCP tool definitions.

Each docstring here is read by the LLM agent to decide when to invoke the tool.
Treat it as UX copy, not as a code comment.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from transcription_mcp.config import Config
from transcription_mcp.jobs import (
    cancel_transcription_job,
    create_transcription_job_bundle,
    get_transcription_job_artifact,
    get_transcription_job_result,
    get_transcription_job_status,
    run_transcription_job_with_budget,
    start_transcription_job,
    watch_transcription_job,
)


def register_tools(mcp: FastMCP, config: Config) -> None:
    @mcp.tool()
    async def transcribe_youtube(
        url: Annotated[
            str,
            Field(description="Full YouTube URL to transcribe."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
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
        """Transcribe a YouTube video, waiting up to the server's sync budget.

        Short/cached videos finish within the budget and return the final
        result directly (use result.transcript). Longer videos hand off: the
        response carries sync_budget_exceeded=true and a run_id while the job
        KEEPS RUNNING in the background - follow it with watch_transcription;
        do NOT call this tool again for the same source (duplicate cost).

        The provider order is decided by the server (default Groq -> ElevenLabs ->
        YouTube captions); it is not a client option. The response reports the
        order actually used in `provider_order_effective`.
        """
        return await run_transcription_job_with_budget(
            budget_seconds=config.sync_tool_budget_seconds,
            source=url,
            source_type="youtube",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=config.youtube_provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            ytdlp_cookies_file=config.ytdlp_cookies_file,
            ytdlp_proxy=config.ytdlp_proxy,
            cache_ttl_hours=config.cache_ttl_hours,
            max_concurrent_jobs=config.max_concurrent_jobs,
            job_ttl_hours=config.job_ttl_hours,
        )

    @mcp.tool()
    async def transcribe_media_url(
        url: Annotated[
            str,
            Field(description="Public media URL supported by yt-dlp or a direct provider source_url."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
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
        """Transcribe a public media URL, waiting up to the server's sync budget.

        Audio providers only (no YouTube captions fallback). Short/cached
        sources return the final result directly (use result.transcript);
        longer ones hand off with sync_budget_exceeded=true and a run_id while
        the job KEEPS RUNNING - follow it with watch_transcription; do NOT call
        this tool again for the same source. Provider order is server-side;
        see `provider_order_effective` in the response.
        """
        return await run_transcription_job_with_budget(
            budget_seconds=config.sync_tool_budget_seconds,
            source=url,
            source_type="media_url",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=config.media_provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            ytdlp_cookies_file=config.ytdlp_cookies_file,
            ytdlp_proxy=config.ytdlp_proxy,
            cache_ttl_hours=config.cache_ttl_hours,
            max_concurrent_jobs=config.max_concurrent_jobs,
            job_ttl_hours=config.job_ttl_hours,
        )

    @mcp.tool()
    async def transcribe_file(
        file_path: Annotated[
            str,
            Field(description="Absolute path to a local audio or video file visible to the MCP host."),
        ],
        language: Annotated[
            str | None,
            Field(description="Optional ISO 639-1 language code. Omit for auto-detect."),
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
        """Transcribe a local file, waiting up to the server's sync budget.

        Short/cached files return the final result directly (use
        result.transcript); longer ones hand off with sync_budget_exceeded=true
        and a run_id while the job KEEPS RUNNING - follow it with
        watch_transcription; do NOT call this tool again for the same file.
        Provider order is server-side; see `provider_order_effective`.
        """
        return await run_transcription_job_with_budget(
            budget_seconds=config.sync_tool_budget_seconds,
            source=file_path,
            source_type="file",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=config.file_provider_order,
            diarize=diarize,
            num_speakers=num_speakers,
            cache_ttl_hours=config.cache_ttl_hours,
            max_concurrent_jobs=config.max_concurrent_jobs,
            job_ttl_hours=config.job_ttl_hours,
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

        If an identical job is already active, its run_id is returned instead
        (deduplicated=true) - never start a second job for the same source.
        Agent workflow: show user_visible_message, keep run_id, and follow it
        with watch_transcription until terminal.
        Provider order is server-side; the result reports `provider_order_effective`.
        """
        return start_transcription_job(
            source=url,
            source_type="youtube",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=config.youtube_provider_order,
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

        If an identical job is already active, its run_id is returned instead
        (deduplicated=true) - never start a second job for the same source.
        Agent workflow: show user_visible_message, keep run_id, and follow it
        with watch_transcription until terminal.
        Provider order is server-side; the result reports `provider_order_effective`.
        """
        return start_transcription_job(
            source=url,
            source_type="media_url",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=config.media_provider_order,
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

        If an identical job is already active, its run_id is returned instead
        (deduplicated=true) - never start a second job for the same file.
        Agent workflow: show user_visible_message, keep run_id, and follow it
        with watch_transcription until terminal.
        Provider order is server-side; the result reports `provider_order_effective`.
        """
        return start_transcription_job(
            source=file_path,
            source_type="file",
            language=language,
            workspace_dir=config.workspace_dir,
            provider_order=config.file_provider_order,
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
    async def watch_transcription(
        run_id: Annotated[
            str,
            Field(description="run_id returned by a start_* transcription tool."),
        ],
        since_revision: Annotated[
            int | None,
            Field(
                description=(
                    "The last `revision` you saw. The call returns as soon as the job's "
                    "revision changes. Omit (null) on the first call."
                )
            ),
        ] = None,
        timeout_seconds: Annotated[
            float,
            Field(description="Max seconds to wait for a change before returning (capped at 30). Default 25."),
        ] = 25.0,
    ) -> dict[str, Any]:
        """Long-poll for progress on a background transcription job.

        Blocks until the job reaches a new stage/status (its `revision` changes) or
        until `timeout_seconds`, then returns the current state with `changed`,
        `revision` and `terminal`. Call it again with the returned `revision` as
        `since_revision` to follow progress in a loop WITHOUT yielding the turn.
        Returns immediately if the job is already terminal. Prefer this over
        repeatedly calling get_transcription_status: after start_*, loop
        watch_transcription and show the user each change until terminal, then call
        get_transcription_result.
        """
        return await watch_transcription_job(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
            since_revision=since_revision,
            timeout_seconds=timeout_seconds,
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
    def create_transcription_bundle(
        run_id: Annotated[
            str,
            Field(description="run_id of a COMPLETED transcription job."),
        ],
    ) -> dict[str, Any]:
        """Package a completed transcription into a downloadable .zip and return its paths.

        Use this when the user asks for a FILE / download / attachment of the
        transcription (not just text). The zip bundles transcript, timestamps,
        SRT/VTT subtitles, audit and canonical JSON when available.

        The response includes `bundle_path_for_openclaw`: send THAT file to the
        user as an attachment. Do NOT rebuild files by hand and do NOT send a
        plain .txt as media. If the bundle expired, call this tool again to
        regenerate it. The source of truth stays in the run_dir; this zip is a
        temporary, regenerable copy.
        """
        return create_transcription_job_bundle(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
            openclaw_workspace_dir=config.openclaw_workspace_dir,
            ttl_hours=config.cache_ttl_hours,
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
   If the response has deduplicated=true, an identical job was already active and
   you are now following it - that is success, not an error.
2. Immediately tell the user the job started using user_visible_message and keep run_id.
3. While the job is not terminal, loop watch_transcription (pass the last `revision` as
   since_revision) and report user_visible_message on each change. An unchanged watch
   with a fresh heartbeat means the job is healthy - keep watching. NEVER start another
   transcription for the same source while this one is active.
4. When status is completed, call get_transcription_result with the same run_id.
5. Use result.transcript as the main answer. Use get_transcription_artifact only if the
   user asks for subtitles, timestamps, audit data, or another listed artifact.
6. If status is failed or canceled, stop polling and explain the terminal state using
   user_visible_message plus error, failed_attempts, and logs when present.
"""
