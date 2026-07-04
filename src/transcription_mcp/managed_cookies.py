"""Consumption side of the authgate-managed YouTube cookies.

authgate (a separate service) writes a Netscape cookies file to the shared
workspace volume after a human logs in. This module is how the MCP *uses* that
file: resolving which cookies to hand yt-dlp, keeping the sliding idle-TTL alive
by touching the file on each successful use, and reading its freshness for the
auth-status tool. Intentionally standalone — the MCP never imports authgate.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def is_fresh(path: Path, *, idle_ttl_s: float, now: float | None = None) -> bool:
    """True if the cookies file exists and was used within the idle window."""
    path = Path(path)
    if not path.is_file():
        return False
    if idle_ttl_s <= 0:
        return True
    reference = now if now is not None else time.time()
    return (reference - path.stat().st_mtime) <= idle_ttl_s


def touch(path: Path) -> bool:
    """Slide the idle TTL forward (mark just-used). False if the file is gone."""
    path = Path(path)
    if not path.is_file():
        return False
    now = time.time()
    try:
        os.utime(path, (now, now))
        return True
    except OSError:
        return False


def resolve_cookies_file(
    *,
    explicit: Path | None,
    managed: Path | None,
    idle_ttl_s: float,
    now: float | None = None,
) -> tuple[Path | None, bool]:
    """Pick the cookies file yt-dlp should use.

    Precedence:
      1. An explicit operator-provided file (YT_COOKIES_FILE) always wins.
      2. Otherwise the authgate-managed file, but only while it is fresh.
      3. Otherwise none (yt-dlp runs cookie-less).

    Returns (path_or_none, used_managed) so the caller can touch the managed
    file's TTL only when the managed file is the one actually in play.
    """
    if explicit is not None:
        return explicit, False
    if managed is not None and is_fresh(managed, idle_ttl_s=idle_ttl_s, now=now):
        return Path(managed), True
    return None, False
