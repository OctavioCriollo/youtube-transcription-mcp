"""Thin client for the authgate service's internal API.

Small, synchronous, and forgiving: authgate is optional infrastructure, so
every call degrades to a structured "unavailable" result instead of raising,
letting the login tools stay callable even when the service is down or unset.
"""

from __future__ import annotations

from typing import Any

import httpx


class AuthgateUnavailable(RuntimeError):
    """authgate is not configured or could not be reached."""


class AuthgateClient:
    def __init__(self, base_url: str | None, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return self._base_url is not None

    def open_login(self) -> dict[str, Any]:
        """Open (or reuse) a login session. Returns the session payload.

        The payload includes `login_path` (relative) while the window is open;
        the caller prepends the public base URL to form the human link.
        """
        return self._request("POST", "/internal/sessions")

    def active_status(self) -> dict[str, Any]:
        """Return the current active session, or {'active': False}."""
        return self._request("GET", "/internal/active")

    def _request(self, method: str, path: str) -> dict[str, Any]:
        if self._base_url is None:
            raise AuthgateUnavailable("AUTHGATE_BASE_URL is not set")
        url = f"{self._base_url}{path}"
        try:
            response = httpx.request(method, url, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthgateUnavailable(f"authgate request failed: {exc}") from exc
