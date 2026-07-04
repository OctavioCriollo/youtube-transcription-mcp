"""Response/guidance builders for the YouTube remote-login tools.

Kept separate from the FastMCP tool wrappers so the branching (configured?
reachable? already authenticated? still logging in?) is unit-testable without a
server. Every return value follows the same agent-guidance shape the rest of
the MCP uses: a user_visible_message plus explicit agent_instructions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from transcription_mcp.authgate_client import AuthgateClient, AuthgateUnavailable
from transcription_mcp.managed_cookies import is_fresh


def request_login(
    client: AuthgateClient,
    *,
    public_base: str | None,
    managed_cookies_file: Path,
    idle_ttl_s: float,
) -> dict[str, Any]:
    """Open a remote login session and return a link for the human."""
    # If we already hold fresh cookies, a new login is pointless.
    if is_fresh(managed_cookies_file, idle_ttl_s=idle_ttl_s):
        return {
            "status": "already_authenticated",
            "user_visible_message": (
                "YouTube is already authenticated on the server; no login needed. "
                "The cheap transcription tier is available."
            ),
            "agent_instructions": [
                "Do not ask the user to log in; valid cookies already exist.",
                "Just run the transcription.",
            ],
        }

    if not client.configured:
        return _unavailable()

    try:
        session = client.open_login()
    except AuthgateUnavailable as exc:
        return _unavailable(str(exc))

    login_path = session.get("login_path")
    if not login_path:
        # Session is active but already past the interactive phase.
        return {
            "status": "pending",
            "session_id": session.get("id"),
            "user_visible_message": "A login session is already in progress.",
            "recommended_next_tool": "get_youtube_auth_status",
            "agent_instructions": [
                "Poll get_youtube_auth_status; do not open another login.",
            ],
        }

    login_url = f"{public_base.rstrip('/')}{login_path}" if public_base else None
    payload: dict[str, Any] = {
        "status": "login_required",
        "session_id": session.get("id"),
        "expires_at": session.get("expires_at"),
        "recommended_next_tool": "get_youtube_auth_status",
        "recommended_poll_seconds": 15,
    }
    if login_url:
        payload["login_url"] = login_url
        payload["user_visible_message"] = (
            f"To transcribe from this server we need a YouTube session. Open this "
            f"link and sign in with the disposable Google account, then tell me when "
            f"you're done:\n{login_url}\nThe link stops working once you finish or "
            f"after the login window closes."
        )
        payload["agent_instructions"] = [
            "Send login_url to the user over the chat channel.",
            "Then poll get_youtube_auth_status every recommended_poll_seconds.",
            "When it reports authenticated, retry the transcription; the Groq tier "
            "will work.",
            "Never ask for or handle the user's password; they log in themselves in "
            "the remote browser.",
        ]
    else:
        # authgate is up but the operator did not set the public base URL.
        payload["status"] = "misconfigured"
        payload["login_path"] = login_path
        payload["user_visible_message"] = (
            "A login session started but the server's public login URL is not "
            "configured (AUTHGATE_PUBLIC_LOGIN_BASE), so I can't build a link."
        )
        payload["agent_instructions"] = [
            "Tell the user remote login is half-configured and an operator must set "
            "AUTHGATE_PUBLIC_LOGIN_BASE.",
        ]
    return payload


def auth_status(
    client: AuthgateClient,
    *,
    managed_cookies_file: Path,
    idle_ttl_s: float,
) -> dict[str, Any]:
    """Report whether YouTube auth is ready, in progress, or needed."""
    cookies_valid = is_fresh(managed_cookies_file, idle_ttl_s=idle_ttl_s)
    if cookies_valid:
        return {
            "status": "authenticated",
            "cookies_valid": True,
            "user_visible_message": (
                "YouTube is authenticated; the cheap transcription tier is available."
            ),
            "agent_instructions": [
                "Cookies are valid. Run or retry the transcription now.",
            ],
        }

    active = {"active": False}
    if client.configured:
        try:
            active = client.active_status()
        except AuthgateUnavailable:
            active = {"active": False}

    if active.get("active") and active.get("state") in {"launching", "awaiting_login"}:
        return {
            "status": "awaiting_login",
            "cookies_valid": False,
            "session_id": active.get("id"),
            "recommended_next_tool": "get_youtube_auth_status",
            "recommended_poll_seconds": 15,
            "user_visible_message": (
                "Still waiting for you to finish the login in the link I sent. "
                "Once you're signed in, this will flip to authenticated."
            ),
            "agent_instructions": [
                "Keep polling get_youtube_auth_status every recommended_poll_seconds.",
                "Do not open a second login session.",
            ],
        }

    return {
        "status": "needs_login",
        "cookies_valid": False,
        "recommended_next_tool": "request_youtube_login",
        "user_visible_message": (
            "No active YouTube session. Ask me to start a login if you want the "
            "cheaper transcription tier."
        ),
        "agent_instructions": [
            "Call request_youtube_login to get a link for the user.",
        ],
    }


def _unavailable(detail: str | None = None) -> dict[str, Any]:
    message = "Remote YouTube login is not available on this server."
    return {
        "status": "unavailable",
        "error": detail,
        "user_visible_message": (
            message + " Transcription still works via the cloud tier (ElevenLabs)."
        ),
        "agent_instructions": [
            "Do not promise a login link.",
            "Proceed with transcription; it will use the cloud fallback tier.",
        ],
    }
