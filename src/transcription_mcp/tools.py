"""MCP tool definitions.

Each docstring here is read by the LLM agent (OpenClaw) to decide when to
invoke the tool. Treat it as UX copy, not as a code comment.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from transcription_mcp.config import Config
from transcription_mcp.jobs import (
    cancel_transcription_job,
    get_transcription_job_result,
    get_transcription_job_status,
    start_transcription_job,
)
from transcription_mcp.pipeline import transcribe_youtube_sync


def register_tools(mcp: FastMCP, config: Config) -> None:
    @mcp.tool()
    def transcribe_youtube(
        url: Annotated[
            str,
            Field(
                description=(
                    "Full YouTube URL to transcribe. Accepts youtube.com/watch?v=..., "
                    "youtu.be/..., and youtube.com/shorts/... forms."
                )
            ),
        ],
        language: Annotated[
            str | None,
            Field(
                description=(
                    "Optional ISO 639-1 language code (e.g. 'es', 'en', 'pt'). "
                    "Leave unset for automatic detection — the default and recommended path. "
                    "Only set when you are certain of the spoken language. Does NOT translate."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Transcribe the audio of a YouTube video into text.

        Use this when the user shares a YouTube link and asks for the
        transcript, the text, what was said, a summary (you summarise the
        returned transcript), or any similar request requiring the spoken
        content.

        Three transcription methods are tried in order; first success wins:
          1. **Groq Whisper** (cheap, ~$0.04/hr). Requires downloading the
             audio with yt-dlp first. Often blocked on cloud VPS IPs.
          2. **ElevenLabs Scribe v2** (~$0.22/hr). ElevenLabs downloads
             the YouTube URL on their infrastructure, bypassing IP blocks.
             Higher quality than Groq.
          3. **YouTube auto-captions / CC** (free, degraded). Last resort
             when both pay providers fail. Often no punctuation, errors
             on proper nouns.

        How to present the result to the user, based on the `method` field
        in the response:
          - `method == "groq"`: deliver the transcript normally. Cheapest
            and good quality.
          - `method == "elevenlabs"`: deliver normally. Mention that Groq
            was unavailable for this video (you can see why in
            `failed_attempts.groq` if helpful) — quality is still high.
          - `method == "subtitles"`: deliver the transcript AND warn the
            user that BOTH pay providers failed (causes in
            `failed_attempts`), so the result comes from YouTube auto-CC
            which may be lower quality.

        Returns: transcript, language (detected), duration_s, model,
        provider, estimated_cost_usd, youtube metadata, method, audit
        summary, and (when earlier levels failed) `failed_attempts`.

        Latency expectations:
          - Groq path: under 60 seconds for short videos.
          - ElevenLabs path: under 90 seconds.
          - Subtitles path: under 5 seconds.

        Failure: if ALL three paths fail (e.g., private video AND no
        API keys configured), the error message lists each attempt and
        its failure reason.
        """
        return transcribe_youtube_sync(
            url=url,
            language=language,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def start_youtube_transcription(
        url: Annotated[
            str,
            Field(
                description=(
                    "Full YouTube URL to transcribe asynchronously. Use this for long videos "
                    "or when the user needs visible progress instead of a blocking call."
                )
            ),
        ],
        language: Annotated[
            str | None,
            Field(
                description=(
                    "Optional ISO 639-1 language code (e.g. 'es', 'en', 'pt'). "
                    "Leave unset for automatic detection."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Start a background YouTube transcription job and return a run_id.

        This is the production-safe flow for long videos. The tool returns
        quickly with `run_id`; call `get_transcription_status(run_id)` until
        `status == "completed"`, then call `get_transcription_result(run_id)`.
        """
        return start_transcription_job(
            url=url,
            language=language,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def get_transcription_status(
        run_id: Annotated[
            str,
            Field(description="run_id returned by start_youtube_transcription."),
        ],
    ) -> dict[str, Any]:
        """Return persisted status for a background transcription job."""
        return get_transcription_job_status(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def get_transcription_result(
        run_id: Annotated[
            str,
            Field(description="run_id returned by start_youtube_transcription."),
        ],
    ) -> dict[str, Any]:
        """Return the final transcript/result for a completed background job."""
        return get_transcription_job_result(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
        )

    @mcp.tool()
    def cancel_transcription(
        run_id: Annotated[
            str,
            Field(description="run_id returned by start_youtube_transcription."),
        ],
    ) -> dict[str, Any]:
        """Best-effort cancellation for a running background transcription job."""
        return cancel_transcription_job(
            run_id=run_id,
            workspace_dir=config.workspace_dir,
        )
