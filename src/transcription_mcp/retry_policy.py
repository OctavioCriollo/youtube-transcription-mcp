"""Error classification for the provider fallback chain (corrective item 3).

Before this module, ANY exception escalated to the next provider tier. That
conflates three very different situations:

- BLOCKED:      YouTube refused us (403, bot check, geo block). Retrying the
                same tier is pointless -> escalate immediately.
- RATE_LIMITED: the provider said "slow down" (429 / quota). Escalating from
                Groq to ElevenLabs here multiplies cost ~5x for an error that
                resolves itself in seconds -> wait, then retry the same tier.
- TRANSIENT:    network hiccups (timeouts, resets, DNS, 5xx). These resolve
                on their own -> short backoff+jitter retry on the same tier.
- FATAL:        everything else (bad input, missing binary, auth failure).
                Retrying cannot help -> escalate immediately.

Classification is message-based on purpose: errors arrive wrapped by yt-dlp,
httpx, Groq and ElevenLabs adapters with inconsistent exception types, but the
underlying signatures (status codes, canonical phrases) survive the wrapping.
The full exception chain (__cause__ / __context__) is inspected so wrappers do
not hide the root cause.
"""

from __future__ import annotations

import os
import random
from enum import Enum


class ErrorClass(str, Enum):
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    FATAL = "fatal"


_BLOCKED_SIGNATURES = (
    "http error 403",
    "403 forbidden",
    "sign in to confirm",
    "confirm you're not a bot",
    "not a bot",
    "video unavailable in your country",
    "not available in your country",
    "access denied",
    "ip has been blocked",
    "unusual traffic",
)

_RATE_LIMIT_SIGNATURES = (
    "http error 429",
    "429",
    "too many requests",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "over capacity",
)

_TRANSIENT_SIGNATURES = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "temporary failure in name resolution",
    "name or service not known",
    "remote end closed connection",
    "eof occurred",
    "incomplete read",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "internal server error",
    "ssl",
    "network is unreachable",
)


def _messages_in_chain(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " | ".join(parts).lower()


def classify_exception(exc: BaseException) -> ErrorClass:
    text = _messages_in_chain(exc)
    # Order matters: rate-limit markers ("429") can coexist with generic
    # words that also appear in transient signatures, and blocked markers
    # must win over transient ones ("403" responses often mention SSL etc.).
    for signature in _RATE_LIMIT_SIGNATURES:
        if signature in text:
            return ErrorClass.RATE_LIMITED
    for signature in _BLOCKED_SIGNATURES:
        if signature in text:
            return ErrorClass.BLOCKED
    for signature in _TRANSIENT_SIGNATURES:
        if signature in text:
            return ErrorClass.TRANSIENT
    return ErrorClass.FATAL


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def max_retries_for(error_class: ErrorClass) -> int:
    """How many same-tier retries a given error class earns.

    BLOCKED / FATAL always return 0: retrying cannot change the outcome.
    """
    if error_class is ErrorClass.TRANSIENT:
        return _int_env("MCP_RETRY_TRANSIENT", 2)
    if error_class is ErrorClass.RATE_LIMITED:
        return _int_env("MCP_RETRY_RATE_LIMITED", 2)
    return 0


def backoff_seconds(error_class: ErrorClass, attempt: int) -> float:
    """Wait before retry `attempt` (1-based), with jitter to avoid thundering herd.

    Transient errors resolve fast: 2s, 4s (+ jitter).
    Rate limits need breathing room: 15s, 30s (+ jitter), capped at 60s.
    """
    if error_class is ErrorClass.RATE_LIMITED:
        base = min(60.0, 15.0 * attempt)
    else:
        base = min(10.0, 2.0**attempt)
    return base + random.uniform(0.0, 1.5)
