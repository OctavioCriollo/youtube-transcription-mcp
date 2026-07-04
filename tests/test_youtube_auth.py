"""YouTube remote-login integration on the MCP side (v0.4.0).

Covers cookie resolution/precedence, the login/status tool guidance, and the
pipeline hint that tells the agent a login would restore the cheap tier.
"""

from __future__ import annotations

import os
import time

from transcription_mcp import youtube_login
from transcription_mcp.authgate_client import AuthgateUnavailable
from transcription_mcp.managed_cookies import is_fresh, resolve_cookies_file, touch
from transcription_mcp.pipeline import _youtube_login_hint

SIGN_IN = (
    "[blocked] YoutubeDownloadError: yt-dlp failed to download audio: ERROR: "
    "[youtube] abc: Sign in to confirm you're not a bot."
)


# --- managed cookie resolution ---------------------------------------------


def test_explicit_cookies_win_over_managed(tmp_path):
    explicit = tmp_path / "explicit.txt"
    managed = tmp_path / "managed.txt"
    explicit.write_text("x", encoding="utf-8")
    managed.write_text("y", encoding="utf-8")

    resolved, used_managed = resolve_cookies_file(
        explicit=explicit, managed=managed, idle_ttl_s=3600
    )
    assert resolved == explicit
    assert used_managed is False


def test_managed_used_only_when_fresh(tmp_path):
    managed = tmp_path / "managed.txt"
    managed.write_text("y", encoding="utf-8")

    resolved, used_managed = resolve_cookies_file(
        explicit=None, managed=managed, idle_ttl_s=3600
    )
    assert resolved == managed
    assert used_managed is True

    stale = time.time() - 7200
    os.utime(managed, (stale, stale))
    resolved, used_managed = resolve_cookies_file(
        explicit=None, managed=managed, idle_ttl_s=3600
    )
    assert resolved is None
    assert used_managed is False


def test_touch_slides_freshness_forward(tmp_path):
    managed = tmp_path / "managed.txt"
    managed.write_text("y", encoding="utf-8")
    old = time.time() - 1800
    os.utime(managed, (old, old))

    assert touch(managed) is True
    assert is_fresh(managed, idle_ttl_s=3600)


# --- login tool guidance ----------------------------------------------------


class _FakeClient:
    def __init__(self, *, configured=True, session=None, active=None, raises=False):
        self._configured = configured
        self._session = session or {}
        self._active = active or {"active": False}
        self._raises = raises

    @property
    def configured(self):
        return self._configured

    def open_login(self):
        if self._raises:
            raise AuthgateUnavailable("boom")
        return self._session

    def active_status(self):
        if self._raises:
            raise AuthgateUnavailable("boom")
        return self._active


def test_request_login_builds_full_url(tmp_path):
    client = _FakeClient(
        session={"id": "ytauth_1", "state": "launching", "login_path": "/s/tok/vnc.html", "expires_at": 999}
    )
    out = youtube_login.request_login(
        client,
        public_base="https://host/ytauth",
        managed_cookies_file=tmp_path / "none.txt",
        idle_ttl_s=3600,
    )
    assert out["status"] == "login_required"
    assert out["login_url"] == "https://host/ytauth/s/tok/vnc.html"
    assert "token" not in out
    assert out["recommended_next_tool"] == "get_youtube_auth_status"


def test_request_login_short_circuits_when_already_authenticated(tmp_path):
    cookies = tmp_path / "youtube-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    client = _FakeClient()

    out = youtube_login.request_login(
        client, public_base="https://host/ytauth", managed_cookies_file=cookies, idle_ttl_s=3600
    )
    assert out["status"] == "already_authenticated"


def test_request_login_reports_unavailable_when_not_configured(tmp_path):
    client = _FakeClient(configured=False)
    out = youtube_login.request_login(
        client, public_base=None, managed_cookies_file=tmp_path / "none.txt", idle_ttl_s=3600
    )
    assert out["status"] == "unavailable"


def test_request_login_flags_missing_public_base(tmp_path):
    client = _FakeClient(
        session={"id": "s", "state": "launching", "login_path": "/s/tok/vnc.html"}
    )
    out = youtube_login.request_login(
        client, public_base=None, managed_cookies_file=tmp_path / "none.txt", idle_ttl_s=3600
    )
    assert out["status"] == "misconfigured"
    assert "login_url" not in out


def test_auth_status_authenticated_when_cookies_fresh(tmp_path):
    cookies = tmp_path / "youtube-cookies.txt"
    cookies.write_text("data", encoding="utf-8")
    out = youtube_login.auth_status(
        _FakeClient(), managed_cookies_file=cookies, idle_ttl_s=3600
    )
    assert out["status"] == "authenticated"
    assert out["cookies_valid"] is True


def test_auth_status_awaiting_login_when_session_active(tmp_path):
    client = _FakeClient(active={"active": True, "state": "awaiting_login", "id": "s1"})
    out = youtube_login.auth_status(
        client, managed_cookies_file=tmp_path / "absent.txt", idle_ttl_s=3600
    )
    assert out["status"] == "awaiting_login"
    assert out["recommended_poll_seconds"] == 15


def test_auth_status_needs_login_when_nothing_active(tmp_path):
    out = youtube_login.auth_status(
        _FakeClient(active={"active": False}),
        managed_cookies_file=tmp_path / "absent.txt",
        idle_ttl_s=3600,
    )
    assert out["status"] == "needs_login"
    assert out["recommended_next_tool"] == "request_youtube_login"


# --- pipeline hint ----------------------------------------------------------


def test_hint_fires_on_botwall_without_cookies():
    hint = _youtube_login_hint({"groq": SIGN_IN}, cookies_in_effect=False)
    assert hint["youtube_login_would_help"] is True
    assert "restores the cheaper Groq tier" in hint["youtube_login_message"]


def test_hint_notes_stale_cookies_when_blocked_with_cookies():
    hint = _youtube_login_hint({"groq": SIGN_IN}, cookies_in_effect=True)
    assert hint["youtube_login_would_help"] is True
    assert "stale" in hint["youtube_login_message"]


def test_hint_absent_for_unrelated_groq_failure():
    assert _youtube_login_hint({"groq": "[transient] timeout"}, cookies_in_effect=False) == {}


def test_hint_absent_when_groq_not_attempted():
    assert _youtube_login_hint({"elevenlabs": "boom"}, cookies_in_effect=False) == {}
