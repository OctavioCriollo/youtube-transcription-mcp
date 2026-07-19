"""Per-provider circuit breaker (corrective item 4).

When a host's IP is blocked by YouTube, every job burns 30-60s in yt-dlp
timeouts on the Groq tier before escalating — and hammering YouTube from a
blocked IP makes the block worse. The breaker short-circuits that: after N
consecutive BLOCKED-class failures on a provider, the provider is skipped
outright for a cooldown window, and the chain goes straight to the next tier.

Scope decisions:
- Only BLOCKED-class failures open the breaker. Transient/rate-limited errors
  are already handled by same-tier retries (item 3), and fatal errors (bad
  input, missing binary) say nothing about the provider's health.
- Any success closes the breaker and resets the failure count.
- State lives in a JSON file in the workspace, NOT in memory: jobs run as
  subprocesses, so memory is not shared between jobs nor with the server's
  /health route. Writes are atomic (temp + os.replace). Concurrent
  read-modify-write races can lose an increment; that is acceptable for a
  heuristic — the breaker converges after the next failure.

Environment knobs:
    MCP_BREAKER_THRESHOLD   consecutive blocked failures to open (default 3)
    MCP_BREAKER_COOLDOWN_S  seconds the breaker stays open (default 300)
    MCP_BREAKER_ENABLED     set to 0 to disable entirely (default 1)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_STATE_FILENAME = "circuit_breaker.json"


def _enabled() -> bool:
    return os.environ.get("MCP_BREAKER_ENABLED", "1").strip() != "0"


def _threshold() -> int:
    try:
        return max(1, int(os.environ.get("MCP_BREAKER_THRESHOLD", "3")))
    except ValueError:
        return 3


def _cooldown_s() -> float:
    try:
        return max(1.0, float(os.environ.get("MCP_BREAKER_COOLDOWN_S", "300")))
    except ValueError:
        return 300.0


def _state_path(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / _STATE_FILENAME


def _load(workspace_dir: Path) -> dict[str, Any]:
    path = _state_path(workspace_dir)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(workspace_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(workspace_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".cb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        # Best effort: a breaker that cannot persist must never break jobs.
        pass


def _provider_entry(state: dict[str, Any], provider: str) -> dict[str, Any]:
    entry = state.get(provider)
    if not isinstance(entry, dict):
        entry = {}
    entry.setdefault("consecutive_blocked", 0)
    entry.setdefault("open_until", 0.0)
    entry.setdefault("total_successes", 0)
    entry.setdefault("total_failures", 0)
    return entry


def seconds_remaining(workspace_dir: Path, provider: str) -> float:
    """> 0 means the breaker is open and the provider should be skipped."""
    if not _enabled():
        return 0.0
    entry = _provider_entry(_load(workspace_dir), provider)
    return max(0.0, float(entry.get("open_until", 0.0)) - time.time())


def record_success(workspace_dir: Path, provider: str) -> None:
    if not _enabled():
        return
    state = _load(workspace_dir)
    entry = _provider_entry(state, provider)
    entry["consecutive_blocked"] = 0
    entry["open_until"] = 0.0
    entry["total_successes"] = int(entry.get("total_successes", 0)) + 1
    entry["last_success_at"] = time.time()
    state[provider] = entry
    _save(workspace_dir, state)


def record_blocked_failure(workspace_dir: Path, provider: str) -> float:
    """Register a BLOCKED-class failure. Returns cooldown seconds if it opened."""
    if not _enabled():
        return 0.0
    state = _load(workspace_dir)
    entry = _provider_entry(state, provider)
    entry["consecutive_blocked"] = int(entry.get("consecutive_blocked", 0)) + 1
    entry["total_failures"] = int(entry.get("total_failures", 0)) + 1
    entry["last_failure_at"] = time.time()
    opened_for = 0.0
    if entry["consecutive_blocked"] >= _threshold():
        opened_for = _cooldown_s()
        entry["open_until"] = time.time() + opened_for
    state[provider] = entry
    _save(workspace_dir, state)
    return opened_for


def record_other_failure(workspace_dir: Path, provider: str) -> None:
    """Non-blocked failures update stats but never open the breaker."""
    if not _enabled():
        return
    state = _load(workspace_dir)
    entry = _provider_entry(state, provider)
    entry["total_failures"] = int(entry.get("total_failures", 0)) + 1
    entry["last_failure_at"] = time.time()
    state[provider] = entry
    _save(workspace_dir, state)


def last_failure_at(workspace_dir: Path, provider: str) -> float:
    """Unix timestamp of the provider's last recorded failure (0.0 if none)."""
    entry = _provider_entry(_load(workspace_dir), provider)
    try:
        return float(entry.get("last_failure_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def reset(workspace_dir: Path, provider: str) -> None:
    """Close the breaker and clear the blocked streak (world-changed reset).

    Used when the environment demonstrably changed since the failures that
    opened the breaker - e.g. a human just minted fresh YouTube cookies, which
    invalidates every "blocked as bot" data point the breaker accumulated.
    Totals are kept; only the open state and streak are cleared.
    """
    if not _enabled():
        return
    state = _load(workspace_dir)
    entry = _provider_entry(state, provider)
    entry["consecutive_blocked"] = 0
    entry["open_until"] = 0.0
    state[provider] = entry
    _save(workspace_dir, state)


def snapshot(workspace_dir: Path) -> dict[str, Any]:
    """Read-only view for /health: per-provider stats + open/closed status."""
    now = time.time()
    result: dict[str, Any] = {}
    for provider, raw in _load(workspace_dir).items():
        if not isinstance(raw, dict):
            continue
        remaining = max(0.0, float(raw.get("open_until", 0.0)) - now)
        result[provider] = {
            "open": remaining > 0,
            "cooldown_remaining_s": round(remaining, 1),
            "consecutive_blocked": int(raw.get("consecutive_blocked", 0)),
            "total_successes": int(raw.get("total_successes", 0)),
            "total_failures": int(raw.get("total_failures", 0)),
        }
    return result
