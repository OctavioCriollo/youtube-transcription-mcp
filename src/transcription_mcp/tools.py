"""MCP tool definitions.

Each docstring here is read by the LLM agent to decide when to invoke the tool.
Treat it as UX copy, not as a code comment.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from transcription_mcp import circuit_breaker, transcription_plan, youtube_login
from transcription_mcp.authgate_client import AuthgateClient, AuthgateUnavailable
from transcription_mcp.config import Config
from transcription_mcp.managed_cookies import is_fresh as _cookies_fresh
from transcription_mcp.pipeline import parse_provider_order
from transcription_mcp.youtube_subtitles import canonical_youtube_url
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

        Recommended first step: call get_transcription_plan(url) once. If it
        returns recommendation "login_recommended", handle the login per its
        guidance BEFORE starting (so the fast/cheap tier works); for "ready",
        "cached", "login_in_progress" or "fallback_only", just call this tool.
        This keeps the login transparent - the user is only ever prompted when
        no session exists to reuse.

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
            managed_cookies_file=config.managed_cookies_file,
            managed_cookies_idle_ttl_s=config.managed_cookies_idle_ttl_s,
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
            managed_cookies_file=config.managed_cookies_file,
            managed_cookies_idle_ttl_s=config.managed_cookies_idle_ttl_s,
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

        Recommended first step: call get_transcription_plan(url) once and act on
        its recommendation (handle login only when it says "login_recommended";
        otherwise start directly). This keeps the login transparent to the user.

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
            managed_cookies_file=config.managed_cookies_file,
            managed_cookies_idle_ttl_s=config.managed_cookies_idle_ttl_s,
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
            managed_cookies_file=config.managed_cookies_file,
            managed_cookies_idle_ttl_s=config.managed_cookies_idle_ttl_s,
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

    def _authgate_client() -> AuthgateClient:
        return AuthgateClient(config.authgate_base_url)

    @mcp.tool()
    def request_youtube_login() -> dict[str, Any]:
        """Start a remote YouTube login and get a link to send the user.

        Call this when a transcription reports youtube_login_would_help=true (the
        Groq tier was blocked as bot traffic because this server has no YouTube
        session). It opens a browser ON THE SERVER and returns login_url: send
        that link to the user, have them sign in with a disposable Google
        account, then poll get_youtube_auth_status. Once authenticated the cheap
        tier works for ~24h of activity. The user's credentials never reach you
        or the server — they type them into the remote browser themselves.
        """
        return youtube_login.request_login(
            _authgate_client(),
            public_base=config.authgate_public_login_base,
            managed_cookies_file=config.managed_cookies_file,
            idle_ttl_s=config.managed_cookies_idle_ttl_s,
        )

    @mcp.tool()
    def get_youtube_auth_status() -> dict[str, Any]:
        """Check whether the server has a usable YouTube session.

        Returns authenticated (cookies valid, cheap tier ready), awaiting_login
        (a login link is open and the user has not finished), or needs_login
        (ask via request_youtube_login). Poll this after sending a login link.
        """
        return youtube_login.auth_status(
            _authgate_client(),
            managed_cookies_file=config.managed_cookies_file,
            idle_ttl_s=config.managed_cookies_idle_ttl_s,
        )

    @mcp.tool()
    def get_transcription_plan(
        url: Annotated[
            str,
            Field(description="The YouTube/media URL (or file path) you intend to transcribe."),
        ],
        source_type: Annotated[
            str,
            Field(description="'youtube' (default), 'media_url', or 'file'."),
        ] = "youtube",
    ) -> dict[str, Any]:
        """Pre-flight a transcription: which tiers run, and is a login needed?

        CALL THIS FIRST for a YouTube/media URL, before starting the
        transcription. It declares the provider tiers in order and marks whether
        the fast/cheap tier needs a one-time YouTube login ON THIS SERVER and
        whether a session already exists — so the login stays transparent to the
        user. Act on `recommendation`:
          - "ready"/"cached": start the transcription; say NOTHING about login.
          - "login_recommended": a session is missing. Offer the user
            request_youtube_login for the fast tier, OR proceed on the fallback
            if they decline. Only ask when there is genuinely no session to reuse.
          - "login_in_progress": a login link is already open; poll
            get_youtube_auth_status, do not open another.
          - "fallback_only": login isn't configured; just transcribe (fallback).
        The user should only ever see a login prompt when no session exists to
        reuse; every other case proceeds automatically.
        """
        stype = (source_type or "youtube").strip().lower()
        if stype == "youtube":
            canon = canonical_youtube_url(url)
            providers = parse_provider_order(config.youtube_provider_order, allow_subtitles=True)
        elif stype == "media_url":
            canon = url.strip()
            providers = parse_provider_order(config.media_provider_order, allow_subtitles=False)
        else:
            canon = url.strip()
            providers = parse_provider_order(config.file_provider_order, allow_subtitles=False)

        # A session is any operator-provided cookies file, or fresh managed cookies.
        session_ready = bool(config.ytdlp_cookies_file) or _cookies_fresh(
            config.managed_cookies_file, idle_ttl_s=config.managed_cookies_idle_ttl_s
        )
        # Is the cheap tier actually bot-walled on THIS IP? Breaker history knows.
        snap = circuit_breaker.snapshot(config.workspace_dir).get("groq", {})
        groq_blocked_here = bool(snap.get("open") or int(snap.get("consecutive_blocked", 0)) > 0)

        client = _authgate_client()
        login_configured = client.configured
        login_in_progress = False
        if login_configured:
            try:
                active = client.active_status()
                login_in_progress = bool(
                    active.get("active")
                    and active.get("state") in {"launching", "awaiting_login"}
                )
            except AuthgateUnavailable:
                login_configured = False

        cached = (
            transcription_plan.has_fresh_cached_result(
                workspace_dir=config.workspace_dir,
                url=canon,
                cache_ttl_hours=config.cache_ttl_hours,
            )
            if stype in {"youtube", "media_url"}
            else False
        )

        return transcription_plan.build_plan(
            source_type=stype,
            provider_order=providers,
            youtube_session_ready=session_ready,
            groq_blocked_here=groq_blocked_here,
            login_in_progress=login_in_progress,
            login_configured=login_configured,
            cached=cached,
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
0. For a YouTube/media URL, call get_transcription_plan(source) FIRST. Act on its
   recommendation: "login_recommended" -> follow its login guidance before starting
   (reuse any existing session automatically; only ask the user when none exists);
   "ready"/"cached"/"login_in_progress"/"fallback_only" -> proceed without mentioning
   login. Keep the login transparent to the user.
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
