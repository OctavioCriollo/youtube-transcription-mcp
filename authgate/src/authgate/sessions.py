"""Login-session state machine with on-disk persistence.

One login session == one attempt for a human to authenticate in the remote
browser. State is persisted to a JSON file so a container restart does not
strand active sessions (a named gap in the earlier prototype). A capability
token gates the human-facing browser URL: random, single-purpose, and expiring
with the session, so a leaked link stops working when the window closes.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# State machine:
#   launching     -> browser is starting
#   awaiting_login -> browser is up; human must log in
#   authenticated  -> cookies captured (terminal, success)
#   expired        -> window elapsed with no login (terminal)
#   failed         -> browser/export error (terminal)
ACTIVE_STATES = {"launching", "awaiting_login"}
TERMINAL_STATES = {"authenticated", "expired", "failed"}


@dataclass
class Session:
    id: str
    token: str
    state: str
    created_at: float
    expires_at: float
    authenticated_at: float | None = None
    cookie_rows: int | None = None
    error: str | None = None

    def is_active(self, *, now: float | None = None) -> bool:
        reference = now if now is not None else time.time()
        return self.state in ACTIVE_STATES and reference < self.expires_at

    def public(self) -> dict[str, Any]:
        # The token is deliberately NOT exposed here; only the internal create
        # response returns it, so it never leaks into status payloads/logs.
        data = asdict(self)
        data.pop("token", None)
        return data


def _now() -> float:
    return time.time()


class SessionStore:
    """Thread-safe session registry persisted to a single JSON file.

    Kept synchronous (threading.Lock, not asyncio) so it is trivially testable
    without an event loop; the aiohttp handlers call it directly and the writes
    are small.
    """

    def __init__(self, state_dir: Path, *, session_ttl_s: int) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / "sessions.json"
        self._session_ttl_s = int(session_ttl_s)
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for entry in raw.get("sessions", []):
            try:
                session = Session(**entry)
            except TypeError:
                continue
            self._sessions[session.id] = session

    def _persist(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"sessions": [asdict(s) for s in self._sessions.values()]}
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # --- queries -----------------------------------------------------------

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def get_by_token(self, token: str) -> Session | None:
        if not token:
            return None
        with self._lock:
            for session in self._sessions.values():
                # Constant-time compare: the token is a bearer capability.
                if secrets.compare_digest(session.token, token):
                    return session
        return None

    def active_session(self, *, now: float | None = None) -> Session | None:
        with self._lock:
            for session in sorted(
                self._sessions.values(), key=lambda s: s.created_at, reverse=True
            ):
                if session.is_active(now=now):
                    return session
        return None

    # --- mutations ---------------------------------------------------------

    def create(self, *, now: float | None = None) -> Session:
        reference = now if now is not None else _now()
        with self._lock:
            session = Session(
                id="ytauth_" + secrets.token_hex(8),
                token=secrets.token_urlsafe(32),
                state="launching",
                created_at=reference,
                expires_at=reference + self._session_ttl_s,
            )
            self._sessions[session.id] = session
            self._persist()
            return session

    def update(self, session_id: str, **changes: Any) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            for key, value in changes.items():
                if hasattr(session, key):
                    setattr(session, key, value)
            self._persist()
            return session

    def mark_authenticated(self, session_id: str, *, cookie_rows: int, now: float | None = None) -> Session | None:
        reference = now if now is not None else _now()
        return self.update(
            session_id,
            state="authenticated",
            authenticated_at=reference,
            cookie_rows=cookie_rows,
            error=None,
        )

    def mark_failed(self, session_id: str, *, error: str) -> Session | None:
        return self.update(session_id, state="failed", error=error)

    def expire_overdue(self, *, now: float | None = None) -> list[str]:
        """Flip active-but-past-deadline sessions to `expired`. Returns their ids."""
        reference = now if now is not None else _now()
        expired: list[str] = []
        with self._lock:
            for session in self._sessions.values():
                if session.state in ACTIVE_STATES and reference >= session.expires_at:
                    session.state = "expired"
                    expired.append(session.id)
            if expired:
                self._persist()
        return expired

    def prune(self, *, max_age_s: int = 86_400, now: float | None = None) -> int:
        """Drop terminal sessions older than max_age_s. Returns count removed."""
        reference = now if now is not None else _now()
        with self._lock:
            drop = [
                sid
                for sid, s in self._sessions.items()
                if s.state in TERMINAL_STATES and (reference - s.created_at) > max_age_s
            ]
            for sid in drop:
                del self._sessions[sid]
            if drop:
                self._persist()
        return len(drop)
