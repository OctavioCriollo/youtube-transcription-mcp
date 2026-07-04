"""Netscape cookie-file serialization and the sliding-TTL lifecycle.

yt-dlp consumes the classic Netscape "cookies.txt" format. Playwright hands us
cookies as dicts; this module converts them and owns the file's lifecycle:
written atomically with 0600 permissions, and reaped after an idle window.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# Minted by authgate for the transcription MCP. Do not edit by hand.\n"
)


def _flag(is_subdomain: bool) -> str:
    return "TRUE" if is_subdomain else "FALSE"


def to_netscape(cookies: Iterable[dict[str, Any]], *, domains: Iterable[str] | None = None) -> str:
    """Render Playwright cookies as a Netscape cookies.txt body.

    If `domains` is given, only cookies whose domain ends with one of those
    suffixes are included (keeps the file to youtube/google and nothing else).
    Session cookies (no real expiry) are written with expiration 0, which
    yt-dlp accepts.
    """
    suffixes = tuple(domains) if domains else ()
    lines: list[str] = [NETSCAPE_HEADER]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").strip()
        name = str(cookie.get("name") or "").strip()
        if not domain or not name:
            continue
        if suffixes and not any(domain == s or domain.endswith(s) for s in suffixes):
            continue
        include_subdomains = domain.startswith(".")
        path = str(cookie.get("path") or "/")
        secure = bool(cookie.get("secure", False))
        expires_raw = cookie.get("expires", 0)
        try:
            expires = int(float(expires_raw))
        except (TypeError, ValueError):
            expires = 0
        if expires < 0:
            expires = 0  # session cookie
        value = str(cookie.get("value") or "")
        lines.append(
            "\t".join(
                [
                    domain,
                    _flag(include_subdomains),
                    path,
                    _flag(secure),
                    str(expires),
                    name,
                    value,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def write_cookies_atomic(
    path: Path,
    cookies: Iterable[dict[str, Any]],
    *,
    domains: Iterable[str] | None = None,
) -> int:
    """Write cookies to `path` atomically with 0600 perms. Returns line count.

    The value is the number of cookie rows written (header excluded), so the
    caller can detect an empty/no-op export.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = to_netscape(cookies, domains=domains)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # non-POSIX filesystems (e.g. Windows dev) — perms are best effort
    tmp.replace(path)
    # Rows = non-comment, non-blank lines.
    return sum(
        1 for line in body.splitlines() if line.strip() and not line.startswith("#")
    )


def touch(path: Path) -> bool:
    """Mark the cookies file as just-used (slides the idle TTL forward).

    Returns False if the file does not exist, so a caller can tell "kept alive"
    from "already gone".
    """
    path = Path(path)
    if not path.is_file():
        return False
    now = time.time()
    os.utime(path, (now, now))
    return True


def is_fresh(path: Path, *, idle_ttl_s: int, now: float | None = None) -> bool:
    """True if the cookies file exists and was used within the idle window."""
    path = Path(path)
    if not path.is_file():
        return False
    if idle_ttl_s <= 0:
        return True
    reference = now if now is not None else time.time()
    return (reference - path.stat().st_mtime) <= idle_ttl_s


def reap_if_idle(path: Path, *, idle_ttl_s: int, now: float | None = None) -> bool:
    """Delete the cookies file if it has been idle past the TTL. Returns True if removed."""
    path = Path(path)
    if not path.is_file() or idle_ttl_s <= 0:
        return False
    reference = now if now is not None else time.time()
    if (reference - path.stat().st_mtime) <= idle_ttl_s:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
