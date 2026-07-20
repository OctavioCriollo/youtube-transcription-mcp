"""Pre-flight plan for a transcription request.

The point of this module is to make the login need TRANSPARENT to the human.
The user just says "transcribe this video". The agent, before running anything,
asks for the plan: it declares each provider tier, marks which one needs a
YouTube session on this server, and whether that session already exists. So the
agent can silently reuse an existing session, ask the user to log in ONLY when
there is genuinely no session, and never make the human think about "Groq needs
cookies". The fallback tiers always run without a login.

Pure logic here (no I/O) so every branch is unit-tested; the tool layer feeds it
the live readiness facts (cookie freshness, breaker history, authgate session).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from transcription_mcp.config import STORAGE_DIR_NAME

# Tiers that download the audio on THIS server with yt-dlp, so a datacenter IP
# needs a YouTube session (cookies) to get past the bot wall. The remote/caption
# tiers never do, so they never need a login.
_LOGIN_TIERS = {"groq", "local"}
_URL_SOURCE_TYPES = {"youtube", "media_url"}


def _provider_note(provider: str, *, requires_login: bool, session_ready: bool) -> str:
    if provider in _LOGIN_TIERS:
        base = "Cheapest/fastest tier. Downloads the audio on this server with yt-dlp."
        if not requires_login:
            return base
        return (
            base
            + (" A YouTube session is present, so it is ready."
               if session_ready
               else " This server's IP is bot-walled by YouTube, so it needs a one-time login.")
        )
    if provider == "elevenlabs":
        return "Cloud fallback. ElevenLabs fetches and transcribes remotely; no login, no server-side download."
    if provider == "subtitles":
        return "Captions fallback. Pulls existing YouTube captions; no login and no audio."
    return "Fallback tier."


def has_fresh_cached_result(
    *,
    workspace_dir: Path,
    url: str,
    cache_ttl_hours: float | None,
) -> bool:
    """Best-effort: is there a recent completed run for this URL?

    Informational only (drives the 'cached' recommendation), so it stays cheap
    and conservative: a finalized run (run.json present) whose mtime is inside
    the cache window counts. Any doubt returns False.
    """
    if not cache_ttl_hours or cache_ttl_hours <= 0:
        return False
    try:
        from transcription_engine.storage import item_id_for_url

        runs_dir = (
            Path(workspace_dir)
            / STORAGE_DIR_NAME
            / "items"
            / item_id_for_url(url)
            / "runs"
        )
        if not runs_dir.is_dir():
            return False
        cutoff = time.time() - cache_ttl_hours * 3600
        for run_dir in runs_dir.iterdir():
            run_json = run_dir / "run.json"
            if run_json.is_file() and run_json.stat().st_mtime >= cutoff:
                return True
    except OSError:
        return False
    return False


def build_plan(
    *,
    source_type: str,
    provider_order: tuple[str, ...],
    youtube_session_ready: bool,
    groq_blocked_here: bool,
    login_in_progress: bool,
    login_configured: bool,
    cached: bool,
    login_url: str | None = None,
) -> dict[str, Any]:
    """Declare the provider options and what (if anything) the human must do."""
    is_url = source_type in _URL_SOURCE_TYPES

    provider_plan: list[dict[str, Any]] = []
    for index, provider in enumerate(provider_order):
        # A download tier needs a login only where the IP is actually bot-walled;
        # on an unblocked IP the same tier works with no session at all. So the
        # operational "requires login" is the structural tier AND evidence of a
        # block here.
        requires_login = is_url and provider in _LOGIN_TIERS and groq_blocked_here
        session_ready = youtube_session_ready if requires_login else True
        provider_plan.append(
            {
                "provider": provider,
                "role": "primary" if index == 0 else "fallback",
                "requires_youtube_login": requires_login,
                "session_ready": session_ready,
                "note": _provider_note(
                    provider, requires_login=requires_login, session_ready=session_ready
                ),
            }
        )

    # Does the CHEAP (primary) tier need a login we don't have?
    primary_needs_login = any(
        p["role"] == "primary" and p["requires_youtube_login"] and not p["session_ready"]
        for p in provider_plan
    )

    recommendation, user_message, instructions = _recommend(
        cached=cached,
        primary_needs_login=primary_needs_login,
        youtube_session_ready=youtube_session_ready,
        groq_blocked_here=groq_blocked_here,
        login_in_progress=login_in_progress,
        login_configured=login_configured,
        login_url=login_url,
    )

    plan: dict[str, Any] = {
        "source_type": source_type,
        "provider_plan": provider_plan,
        "youtube_session_ready": youtube_session_ready,
        "cached": cached,
        "recommendation": recommendation,
        "user_visible_message": user_message,
        "agent_instructions": instructions,
    }
    if login_url and recommendation == "login_recommended":
        plan["login_url"] = login_url
    return plan


def _recommend(
    *,
    cached: bool,
    primary_needs_login: bool,
    youtube_session_ready: bool,
    groq_blocked_here: bool,
    login_in_progress: bool,
    login_configured: bool,
    login_url: str | None,
) -> tuple[str, str, list[str]]:
    if cached:
        return (
            "cached",
            "",
            [
                "A cached result exists; the transcription will return almost instantly.",
                "Just call the transcription tool - no login needed.",
            ],
        )

    if not primary_needs_login:
        # Either the source needs no login, the session already exists, or this
        # IP isn't actually blocked. Nothing for the user to do.
        why = (
            "a YouTube session is already active"
            if youtube_session_ready
            else "the fast tier works from here without one"
        )
        return (
            "ready",
            "",
            [
                f"The fast/low-cost tier is ready ({why}); no login is needed.",
                "Call the transcription tool directly. Do NOT mention login to the user.",
            ],
        )

    if login_in_progress:
        return (
            "login_in_progress",
            "A YouTube login is already open in the link I sent earlier. Finish signing "
            "in there and tell me when you're done.",
            [
                "A login session is already open; do NOT open another.",
                "Poll get_youtube_auth_status; when authenticated, start the transcription.",
            ],
        )

    if not login_configured:
        return (
            "fallback_only",
            "",
            [
                "Remote login is not available on this server, so the fast tier can't be "
                "unlocked here.",
                "Just call the transcription tool; it will use the cloud fallback tier "
                "(slower/pricier but automatic). Do not promise a login.",
            ],
        )

    # The one case where the human is asked - and only once.
    link = f" Here is the link:\n{login_url}" if login_url else ""
    return (
        "login_recommended",
        (
            "To transcribe this from the server on the fast, low-cost tier, YouTube needs "
            "a one-time sign-in (this server's IP is rate-limited by YouTube). It takes a "
            "minute and lasts about a day." + link + "\nIf you'd rather not, tell me and "
            "I'll transcribe it now on the slower cloud tier instead."
        ),
        [
            "Offer the user the choice: log in once now for the fast/cheap tier, or "
            "proceed immediately on the cloud fallback.",
            "If login_url is not present, call request_youtube_login to obtain it, then "
            "send it to the user.",
            "If the user logs in: poll get_youtube_auth_status, then start the "
            "transcription (the cheap tier will work).",
            "If the user declines or doesn't answer: just start the transcription; the "
            "fallback tier runs automatically without a login.",
            "Never ask the user for their password; they sign in themselves in the "
            "remote browser.",
        ],
    )
