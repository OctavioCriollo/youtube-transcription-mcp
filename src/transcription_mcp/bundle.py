"""Create a temporary .zip bundle of a completed transcription run.

The bundle is a convenience artifact for delivery (e.g. OpenClaw sending a file
to the user). It is NOT the source of truth — that remains the run_dir in
storage. The bundle is regenerable and may be cleaned by TTL.

The zip is written inside the run itself:
    <run_dir>/exports/transcription_bundle.zip

so it lives on the same shared volume that OpenClaw mounts read-only. The tool
returns both the MCP-side path and the OpenClaw-side path (when configured), so
the agent can send the file without reconstructing anything.
"""

from __future__ import annotations

import hashlib
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Artifacts included in the bundle if present in the run_dir. Source of truth
# stays in storage; this is just a packaged copy for delivery.
BUNDLE_ARTIFACTS = (
    "transcript.txt",
    "transcript-timestamps.txt",
    "subtitles.srt",
    "subtitles.vtt",
    "audit.txt",
    "audit.json",
    "quality.json",
    "canonical.json",
    "run.json",
)

BUNDLE_FILENAME = "transcription_bundle.zip"


class BundleError(RuntimeError):
    """Raised when a bundle cannot be created (e.g. run_dir missing)."""


def create_bundle(
    *,
    run_dir: Path,
    workspace_dir: Path,
    openclaw_workspace_dir: str | None = None,
    ttl_hours: float | None = 24.0,
) -> dict[str, Any]:
    """Build (or rebuild) the bundle zip for a run and return its metadata."""
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise BundleError(f"run_dir does not exist: {run_dir}")

    exports_dir = run_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    zip_path = exports_dir / BUNDLE_FILENAME

    included: list[str] = []
    # Write atomically: build to a temp file, then replace.
    tmp_path = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in BUNDLE_ARTIFACTS:
            src = run_dir / name
            if src.is_file():
                zf.write(src, arcname=name)
                included.append(name)
    if not included:
        tmp_path.unlink(missing_ok=True)
        raise BundleError(f"no bundleable artifacts found in run_dir: {run_dir}")
    tmp_path.replace(zip_path)

    size_bytes = zip_path.stat().st_size
    sha256 = _sha256_file(zip_path)
    created_at = datetime.now(UTC)
    expires_at = (
        (created_at + timedelta(hours=ttl_hours)).isoformat()
        if ttl_hours and ttl_hours > 0
        else None
    )

    return {
        "status": "completed",
        "filename": BUNDLE_FILENAME,
        "bundle_path_for_mcp": str(zip_path),
        "bundle_path_for_openclaw": _openclaw_path(
            zip_path, workspace_dir, openclaw_workspace_dir
        ),
        "included_artifacts": included,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at,
        "run_dir": str(run_dir),
        "note": (
            "Temporary, regenerable bundle. Source of truth stays in storage. "
            "Send bundle_path_for_openclaw as the file to the user."
        ),
    }


def _openclaw_path(
    zip_path: Path,
    workspace_dir: Path,
    openclaw_workspace_dir: str | None,
) -> str | None:
    """Translate the MCP-side zip path to the path OpenClaw sees (read-only mount).

    The MCP writes under workspace_dir; OpenClaw mounts the same volume at
    openclaw_workspace_dir. We rebase the path so the agent can locate the file
    without knowing the MCP's internal layout.
    """
    if not openclaw_workspace_dir:
        return None
    try:
        relative = zip_path.resolve().relative_to(Path(workspace_dir).resolve())
    except ValueError:
        return None
    base = openclaw_workspace_dir.rstrip("/")
    return f"{base}/{relative.as_posix()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
