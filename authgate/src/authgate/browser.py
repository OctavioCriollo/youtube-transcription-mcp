"""Remote-browser control for the login flow (Playwright, headed on Xvfb).

One session drives one headed Chromium instance rendered into the container's
virtual display (Xvfb :99), which x11vnc mirrors and websockify serves to the
human over noVNC. We poll the browser's cookie jar until a YouTube/Google
session cookie appears, export it, and tear the browser down. The browser only
exists while a login is in progress — deliberately, to bound RAM on a small VPS
and to keep the interactive surface off unless the human is actually using it.

This module is exercised end-to-end only on the server (it needs Chromium and a
display); the pure logic it depends on — cookie serialization, session state —
is unit-tested separately.
"""

from __future__ import annotations

import asyncio
import logging

from authgate.config import AuthgateConfig
from authgate.cookies import write_cookies_atomic
from authgate.sessions import SessionStore

logger = logging.getLogger("authgate.browser")

# Chromium flags required to run inside a container and to render a normal-size
# window into the virtual display.
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-position=0,0",
    "--window-size=1280,800",
]

_POLL_INTERVAL_S = 2.0


def _has_auth_cookie(cookies: list[dict], auth_names: tuple[str, ...]) -> bool:
    wanted = set(auth_names)
    for cookie in cookies:
        if str(cookie.get("name") or "") in wanted and str(cookie.get("value") or ""):
            return True
    return False


async def run_login_session(
    config: AuthgateConfig,
    store: SessionStore,
    session_id: str,
) -> None:
    """Drive a single login session to a terminal state.

    Spawned as a background task when a session is created. Never raises to the
    caller: any failure is recorded on the session as `failed`.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - only in a stripped env
        store.mark_failed(session_id, error=f"playwright unavailable: {exc}")
        return

    session = store.get(session_id)
    if session is None:
        return
    deadline = session.expires_at

    async with async_playwright() as pw:
        browser = None
        try:
            browser = await pw.chromium.launch(headless=False, args=_CHROMIUM_ARGS)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(config.login_start_url, wait_until="domcontentloaded", timeout=60_000)
            store.update(session_id, state="awaiting_login")
            logger.info("session %s: browser up, awaiting login", session_id)

            loop = asyncio.get_event_loop()
            while loop.time() < _remaining_budget(loop, deadline, store, session_id):
                await asyncio.sleep(_POLL_INTERVAL_S)
                current = store.get(session_id)
                if current is None or current.state != "awaiting_login":
                    return  # canceled/expired elsewhere
                try:
                    cookies = await context.cookies()
                except Exception as exc:  # noqa: BLE001 - transient CDP hiccup
                    logger.warning("session %s: cookie read failed: %s", session_id, exc)
                    continue
                if _has_auth_cookie(cookies, config.auth_cookie_names):
                    rows = write_cookies_atomic(
                        config.managed_cookies_file,
                        cookies,
                        domains=config.export_domains,
                    )
                    if rows <= 0:
                        # Auth cookie seen but nothing exportable on our domains;
                        # keep waiting rather than declaring a bad success.
                        continue
                    store.mark_authenticated(session_id, cookie_rows=rows)
                    logger.info("session %s: captured %d cookie rows", session_id, rows)
                    return

            # Fell out of the loop without capturing -> window elapsed.
            store.expire_overdue()
        except Exception as exc:  # noqa: BLE001 - report, never crash the service
            logger.exception("session %s: login run failed", session_id)
            store.mark_failed(session_id, error=f"{type(exc).__name__}: {exc}")
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    logger.warning("session %s: browser close failed", session_id)


def _remaining_budget(loop, deadline: float, store: SessionStore, session_id: str) -> float:
    """Translate the wall-clock deadline into the loop clock, once, defensively.

    We compare against loop.time(); compute the loop-time instant that equals
    the wall-clock deadline. Recomputed each call so clock drift cannot strand
    the loop past the human-visible expiry.
    """
    import time as _time

    wall_remaining = deadline - _time.time()
    return loop.time() + max(0.0, wall_remaining)
