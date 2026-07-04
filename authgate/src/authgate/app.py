"""authgate HTTP surface (aiohttp).

Two audiences, one process:

- The transcription MCP calls the /internal/* API (private, on the MCP docker
  network) to open a login session and to poll its state.
- Traefik calls /auth as a ForwardAuth check for every request to the public
  noVNC path, so only a request carrying a live capability token reaches the
  remote browser. The browser bytes themselves flow Traefik -> websockify
  directly; this service stays out of that data path.

The human-facing login page (noVNC) is served by websockify, not here.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiohttp import web

from authgate.browser import run_login_session
from authgate.config import AuthgateConfig
from authgate.cookies import reap_if_idle
from authgate.sessions import SessionStore

logger = logging.getLogger("authgate.app")

# Token sits in the public path as /ytauth/s/<token>/... ; ForwardAuth receives
# the original URI in X-Forwarded-Uri.
_TOKEN_IN_URI = re.compile(r"/s/([^/?#]+)")

_LOGIN_PATH_TEMPLATE = "/s/{token}/vnc.html?autoconnect=true&resize=remote&reconnect=true"

# Typed application keys (aiohttp's recommended idiom over bare string keys).
CONFIG_KEY: web.AppKey[AuthgateConfig] = web.AppKey("config", AuthgateConfig)
STORE_KEY: web.AppKey[SessionStore] = web.AppKey("store", SessionStore)
BROWSER_TASKS_KEY: web.AppKey[set] = web.AppKey("browser_tasks", set)
JANITOR_KEY: web.AppKey[asyncio.Task] = web.AppKey("janitor", asyncio.Task)


def build_app(config: AuthgateConfig, store: SessionStore) -> web.Application:
    app = web.Application()
    app[CONFIG_KEY] = config
    app[STORE_KEY] = store
    app[BROWSER_TASKS_KEY] = set()

    app.add_routes(
        [
            web.get("/healthz", _healthz),
            web.get("/auth", _forward_auth),
            web.post("/internal/sessions", _create_session),
            web.get("/internal/sessions/{session_id}", _get_session),
            web.get("/internal/active", _get_active),
        ]
    )
    app.on_startup.append(_start_janitor)
    app.on_cleanup.append(_stop_janitor)
    return app


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _forward_auth(request: web.Request) -> web.Response:
    """Traefik ForwardAuth: 200 allows the request through to noVNC, 401 blocks.

    Access is granted only while the token maps to an ACTIVE session (the login
    window is open). Once authenticated/expired, the browser is gone and access
    is denied.
    """
    store = request.app[STORE_KEY]
    uri = request.headers.get("X-Forwarded-Uri", request.path_qs)
    match = _TOKEN_IN_URI.search(uri)
    if not match:
        return web.Response(status=401, text="no token")
    session = store.get_by_token(match.group(1))
    if session is None or not session.is_active():
        return web.Response(status=401, text="invalid or expired token")
    return web.Response(status=200, text="ok")


async def _create_session(request: web.Request) -> web.Response:
    """Open (or reuse) a login session and start the remote browser.

    Single-flight: only one login browser runs at a time (one display, bounded
    RAM). A concurrent request returns the existing active session, so an
    impatient agent cannot spawn a second browser.
    """
    config = request.app[CONFIG_KEY]
    store = request.app[STORE_KEY]

    existing = store.active_session()
    if existing is not None:
        return web.json_response(_session_response(existing), status=200)

    session = store.create()
    task = asyncio.create_task(run_login_session(config, store, session.id))
    tasks = request.app[BROWSER_TASKS_KEY]
    tasks.add(task)
    task.add_done_callback(tasks.discard)

    return web.json_response(_session_response(session), status=201)


async def _get_session(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    session = store.get(request.match_info["session_id"])
    if session is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(_session_response(session))


async def _get_active(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    session = store.active_session()
    if session is None:
        return web.json_response({"active": False})
    return web.json_response({"active": True, **_session_response(session)})


def _session_response(session) -> dict:
    payload = session.public()
    # login_path is relative; the MCP prepends its configured public base URL
    # (e.g. https://host/ytauth) so authgate never needs to know its hostname.
    if session.state in {"launching", "awaiting_login"}:
        payload["login_path"] = _LOGIN_PATH_TEMPLATE.format(token=session.token)
    return payload


# --- background janitor -----------------------------------------------------


async def _start_janitor(app: web.Application) -> None:
    app[JANITOR_KEY] = asyncio.create_task(_janitor_loop(app))


async def _stop_janitor(app: web.Application) -> None:
    task = app.get(JANITOR_KEY)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _janitor_loop(app: web.Application) -> None:
    """Expire overdue login windows, reap idle cookies, prune old sessions."""
    config = app[CONFIG_KEY]
    store = app[STORE_KEY]
    while True:
        try:
            store.expire_overdue()
            store.prune(max_age_s=max(config.cookie_idle_ttl_s, 86_400))
            if reap_if_idle(config.managed_cookies_file, idle_ttl_s=config.cookie_idle_ttl_s):
                logger.info("reaped idle cookies file %s", config.managed_cookies_file)
        except Exception:  # noqa: BLE001 - the janitor must never die
            logger.exception("janitor iteration failed")
        await asyncio.sleep(30)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = AuthgateConfig.from_env()
    store = SessionStore(config.state_dir, session_ttl_s=config.session_ttl_s)
    app = build_app(config, store)
    web.run_app(app, host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
