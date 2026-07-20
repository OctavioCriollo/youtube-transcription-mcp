"""v0.4.4: pre-flight plan makes the YouTube login transparent.

The user just asks to transcribe a video; the agent consults get_transcription_plan,
which declares each provider tier, marks which needs a login on this server, and
whether a session already exists. The user is prompted ONLY when no session can
be reused; every other case proceeds automatically.
"""

from __future__ import annotations

from transcription_mcp.transcription_plan import build_plan

YT_ORDER = ("groq", "elevenlabs", "subtitles")


def _plan(**over):
    base = dict(
        source_type="youtube",
        provider_order=YT_ORDER,
        youtube_session_ready=False,
        groq_blocked_here=True,
        login_in_progress=False,
        login_configured=True,
        cached=False,
    )
    base.update(over)
    return build_plan(**base)


# --- provider_plan shape ----------------------------------------------------


def test_only_primary_download_tier_requires_login():
    plan = _plan()
    by = {p["provider"]: p for p in plan["provider_plan"]}
    assert by["groq"]["role"] == "primary"
    assert by["groq"]["requires_youtube_login"] is True
    # Fallbacks never need a login.
    assert by["elevenlabs"]["requires_youtube_login"] is False
    assert by["subtitles"]["requires_youtube_login"] is False


def test_file_source_needs_no_login_anywhere():
    plan = _plan(source_type="file", provider_order=("groq", "elevenlabs"))
    assert all(not p["requires_youtube_login"] for p in plan["provider_plan"])
    assert plan["recommendation"] == "ready"


# --- recommendation branches ------------------------------------------------


def test_login_recommended_only_when_blocked_and_no_session():
    plan = _plan(youtube_session_ready=False, groq_blocked_here=True, login_configured=True)
    assert plan["recommendation"] == "login_recommended"
    assert "one-time sign-in" in plan["user_visible_message"]
    assert any("request_youtube_login" in i for i in plan["agent_instructions"])
    assert any("declines" in i for i in plan["agent_instructions"])


def test_ready_when_session_exists_says_nothing_to_user():
    plan = _plan(youtube_session_ready=True)
    assert plan["recommendation"] == "ready"
    assert plan["user_visible_message"] == ""
    assert any("Do NOT mention login" in i for i in plan["agent_instructions"])


def test_ready_when_ip_not_blocked_even_without_session():
    # No evidence the cheap tier is blocked here -> don't prompt for login.
    plan = _plan(youtube_session_ready=False, groq_blocked_here=False)
    assert plan["recommendation"] == "ready"


def test_login_in_progress_tells_agent_not_to_open_another():
    plan = _plan(login_in_progress=True)
    assert plan["recommendation"] == "login_in_progress"
    assert any("open another" in i.lower() for i in plan["agent_instructions"])


def test_fallback_only_when_login_not_configured():
    plan = _plan(login_configured=False)
    assert plan["recommendation"] == "fallback_only"
    assert any("cloud fallback" in i for i in plan["agent_instructions"])


def test_cached_short_circuits_everything():
    plan = _plan(cached=True, youtube_session_ready=False, groq_blocked_here=True)
    assert plan["recommendation"] == "cached"
    assert any("instant" in i for i in plan["agent_instructions"])


def test_session_ready_flag_surfaced_at_top_level():
    assert _plan(youtube_session_ready=True)["youtube_session_ready"] is True
    assert _plan(youtube_session_ready=False)["youtube_session_ready"] is False
