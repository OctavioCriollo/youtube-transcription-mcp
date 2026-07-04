from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from authgate import app as app_module
from authgate.config import AuthgateConfig
from authgate.sessions import SessionStore


def _config(tmp_path) -> AuthgateConfig:
    return AuthgateConfig(
        host="127.0.0.1",
        port=8080,
        state_dir=tmp_path / "state",
        managed_cookies_file=tmp_path / "secrets" / "youtube-cookies.txt",
        cookie_idle_ttl_s=86_400,
        session_ttl_s=900,
        login_start_url="https://accounts.google.com/",
        auth_cookie_names=("__Secure-1PSID",),
        export_domains=(".youtube.com", ".google.com"),
        display=":99",
    )


async def _noop_login(config, store, session_id):
    # Stand in for the Playwright browser run: leave the session awaiting_login
    # so /auth can be exercised without a real browser.
    store.update(session_id, state="awaiting_login")


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "run_login_session", _noop_login)

    def make():
        config = _config(tmp_path)
        store = SessionStore(config.state_dir, session_ttl_s=config.session_ttl_s)
        return TestClient(TestServer(app_module.build_app(config, store))), store

    return make


def test_create_then_forward_auth_allows_live_token(client_factory):
    async def run():
        client, store = client_factory()
        async with client:
            created = await client.post("/internal/sessions")
            assert created.status == 201
            body = await created.json()
            assert body["state"] in {"launching", "awaiting_login"}
            assert "login_path" in body
            assert "token" not in body  # token never leaves via the API body

            session = store.active_session()
            token = session.token

            ok = await client.get(
                "/auth", headers={"X-Forwarded-Uri": f"/ytauth/s/{token}/vnc.html"}
            )
            assert ok.status == 200

            bad = await client.get(
                "/auth", headers={"X-Forwarded-Uri": "/ytauth/s/deadbeef/vnc.html"}
            )
            assert bad.status == 401

            none = await client.get("/auth", headers={"X-Forwarded-Uri": "/ytauth/"})
            assert none.status == 401

    asyncio.run(run())


def test_forward_auth_denies_terminal_session(client_factory):
    async def run():
        client, store = client_factory()
        async with client:
            await client.post("/internal/sessions")
            session = store.active_session()
            token = session.token
            store.mark_authenticated(session.id, cookie_rows=3)

            resp = await client.get(
                "/auth", headers={"X-Forwarded-Uri": f"/ytauth/s/{token}/vnc.html"}
            )
            # Browser is gone after auth -> access denied.
            assert resp.status == 401

    asyncio.run(run())


def test_create_is_single_flight(client_factory):
    async def run():
        client, store = client_factory()
        async with client:
            first = await client.post("/internal/sessions")
            assert first.status == 201
            first_id = (await first.json())["id"]

            second = await client.post("/internal/sessions")
            # Reused, not a new browser.
            assert second.status == 200
            assert (await second.json())["id"] == first_id

    asyncio.run(run())


def test_active_endpoint_reports_no_session(client_factory):
    async def run():
        client, _store = client_factory()
        async with client:
            resp = await client.get("/internal/active")
            assert resp.status == 200
            assert (await resp.json()) == {"active": False}

    asyncio.run(run())
