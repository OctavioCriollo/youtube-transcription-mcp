from __future__ import annotations

from authgate.sessions import SessionStore


def _store(tmp_path, ttl=900):
    return SessionStore(tmp_path / "state", session_ttl_s=ttl)


def test_create_returns_token_but_public_hides_it(tmp_path):
    store = _store(tmp_path)
    session = store.create(now=1000.0)
    assert session.token
    assert session.state == "launching"
    assert session.expires_at == 1000.0 + 900
    assert "token" not in session.public()


def test_lookup_by_token_is_exact(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    assert store.get_by_token(session.token).id == session.id
    assert store.get_by_token("wrong") is None
    assert store.get_by_token("") is None


def test_active_session_respects_state_and_deadline(tmp_path):
    store = _store(tmp_path, ttl=100)
    session = store.create(now=1000.0)
    # awaiting_login within window -> active
    store.update(session.id, state="awaiting_login")
    assert store.active_session(now=1050.0) is not None
    # past deadline -> not active
    assert store.active_session(now=1200.0) is None
    # terminal -> not active
    store.mark_authenticated(session.id, cookie_rows=5, now=1050.0)
    assert store.active_session(now=1050.0) is None


def test_mark_authenticated_records_rows(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    updated = store.mark_authenticated(session.id, cookie_rows=7, now=2000.0)
    assert updated.state == "authenticated"
    assert updated.cookie_rows == 7
    assert updated.authenticated_at == 2000.0
    assert updated.error is None


def test_expire_overdue_flips_only_active_past_deadline(tmp_path):
    store = _store(tmp_path, ttl=100)
    a = store.create(now=1000.0)
    store.update(a.id, state="awaiting_login")
    b = store.create(now=1000.0)
    store.mark_authenticated(b.id, cookie_rows=3, now=1050.0)

    expired = store.expire_overdue(now=1200.0)
    assert a.id in expired
    assert b.id not in expired  # already terminal
    assert store.get(a.id).state == "expired"


def test_state_survives_reload_from_disk(tmp_path):
    store = _store(tmp_path)
    session = store.create(now=1000.0)
    store.update(session.id, state="awaiting_login")

    # New instance pointed at the same dir must see the persisted session.
    reloaded = SessionStore(tmp_path / "state", session_ttl_s=900)
    got = reloaded.get(session.id)
    assert got is not None
    assert got.state == "awaiting_login"
    assert reloaded.get_by_token(session.token).id == session.id


def test_prune_drops_old_terminal_sessions_only(tmp_path):
    store = _store(tmp_path)
    old_done = store.create(now=1000.0)
    store.mark_authenticated(old_done.id, cookie_rows=1, now=1000.0)
    active = store.create(now=1000.0)
    store.update(active.id, state="awaiting_login")

    removed = store.prune(max_age_s=3600, now=1000.0 + 7200)
    assert removed == 1
    assert store.get(old_done.id) is None
    assert store.get(active.id) is not None  # active never pruned
