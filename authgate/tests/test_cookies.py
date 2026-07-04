from __future__ import annotations

import time

from authgate.cookies import (
    is_fresh,
    reap_if_idle,
    to_netscape,
    touch,
    write_cookies_atomic,
)


def _cookie(**overrides):
    base = {
        "name": "__Secure-1PSID",
        "value": "abc123",
        "domain": ".youtube.com",
        "path": "/",
        "expires": 1893456000.0,  # 2030
        "secure": True,
        "httpOnly": True,
    }
    base.update(overrides)
    return base


def test_netscape_header_and_row_shape():
    body = to_netscape([_cookie()])
    lines = body.splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    row = [line for line in lines if not line.startswith("#") and line.strip()][0]
    fields = row.split("\t")
    assert fields == [
        ".youtube.com",
        "TRUE",  # leading dot -> include subdomains
        "/",
        "TRUE",  # secure
        "1893456000",
        "__Secure-1PSID",
        "abc123",
    ]


def test_session_cookie_expiry_normalized_to_zero():
    body = to_netscape([_cookie(expires=-1)])
    row = [line for line in body.splitlines() if line.startswith(".youtube")][0]
    assert row.split("\t")[4] == "0"


def test_non_subdomain_and_insecure_flags():
    body = to_netscape([_cookie(domain="www.youtube.com", secure=False)])
    row = [line for line in body.splitlines() if line.startswith("www.youtube")][0]
    fields = row.split("\t")
    assert fields[1] == "FALSE"  # no leading dot
    assert fields[3] == "FALSE"  # insecure


def test_domain_filter_excludes_foreign_cookies():
    cookies = [_cookie(), _cookie(domain=".evil.com", name="tracker")]
    body = to_netscape(cookies, domains=(".youtube.com", ".google.com"))
    assert "evil.com" not in body
    assert "__Secure-1PSID" in body


def test_write_is_atomic_and_counts_rows(tmp_path):
    path = tmp_path / "secrets" / "youtube-cookies.txt"
    rows = write_cookies_atomic(
        path,
        [_cookie(), _cookie(name="SID", domain=".google.com")],
        domains=(".youtube.com", ".google.com"),
    )
    assert rows == 2
    assert path.is_file()
    assert not (path.parent / f"{path.name}.tmp").exists()
    assert "__Secure-1PSID" in path.read_text(encoding="utf-8")


def test_sliding_ttl_touch_fresh_and_reap(tmp_path):
    path = tmp_path / "youtube-cookies.txt"
    write_cookies_atomic(path, [_cookie()])

    # Freshly written -> fresh, not reaped.
    assert is_fresh(path, idle_ttl_s=3600)
    assert reap_if_idle(path, idle_ttl_s=3600) is False

    # Backdate mtime beyond the idle window -> stale, reaped.
    old = time.time() - 7200
    import os

    os.utime(path, (old, old))
    assert is_fresh(path, idle_ttl_s=3600) is False
    assert reap_if_idle(path, idle_ttl_s=3600) is True
    assert not path.is_file()


def test_touch_reports_missing_file(tmp_path):
    path = tmp_path / "nope.txt"
    assert touch(path) is False
    write_cookies_atomic(path, [_cookie()])
    assert touch(path) is True
