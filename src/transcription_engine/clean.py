from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from transcription_engine.status import FINAL_ARTIFACTS, inspect_run


RUN_MARKERS = (
    "run-state.json",
    "run.json",
    "canonical.json",
    "partials",
    "media",
)


def clean_run(
    run_dir: Path,
    *,
    dry_run: bool = True,
    media_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    report = _clean_targets(
        [_target_for_run(run_dir, media_only=media_only)],
        dry_run=dry_run,
        force=force,
    )
    report["scope"] = "run"
    report["media_only"] = media_only
    return report


def clean_incomplete_runs(
    storage_dir: Path,
    *,
    dry_run: bool = True,
    media_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    storage_dir = Path(storage_dir)
    if not storage_dir.exists() or not storage_dir.is_dir():
        raise FileNotFoundError(storage_dir)
    targets = [
        _target_for_run(run_dir, media_only=media_only, storage_root=storage_dir)
        for run_dir in _find_incomplete_runs(storage_dir)
    ]
    report = _clean_targets(targets, dry_run=dry_run, force=force)
    report["scope"] = "incomplete"
    report["storage_dir"] = str(storage_dir)
    report["media_only"] = media_only
    return report


def render_clean_text(report: dict[str, Any]) -> str:
    mode = "dry-run" if report["dry_run"] else "applied"
    lines = [
        "Transcription engine clean",
        "",
        f"mode: {mode}",
        f"scope: {report['scope']}",
        f"media_only: {report['media_only']}",
        f"targets: {len(report['targets'])}",
    ]
    if report.get("storage_dir"):
        lines.append(f"storage_dir: {report['storage_dir']}")
    lines.append("")

    if report["targets"]:
        lines.append("Targets")
        for target in report["targets"]:
            action = target["action"]
            detail = target.get("blocked_reason") or target["target"]
            lines.append(
                f"- {action}: {target['run_dir']} "
                f"(status={target['status']}, stage={target['stage']}, "
                f"size={target['size']}) -> {detail}"
            )
        lines.append("")

    if report["latest_updates"]:
        lines.append("Latest updates")
        for update in report["latest_updates"]:
            lines.append(f"- {update['action']}: {update['latest_path']}")
        lines.append("")

    lines.append(f"removed: {report['removed_count']}")
    lines.append(f"blocked: {report['blocked_count']}")
    if report["blocked_count"]:
        lines.append("Next action: stop the running process, then rerun; use --force only if needed")
    elif report["dry_run"] and report["targets"]:
        lines.append("Next action: rerun with --yes to apply this cleanup")
    else:
        lines.append("Next action: run status or audit as needed")
    return "\n".join(lines).strip() + "\n"


def _clean_targets(
    targets: list[dict[str, Any]],
    *,
    dry_run: bool,
    force: bool,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    latest_updates: list[dict[str, Any]] = []
    removed_count = 0
    blocked_count = 0

    for target in targets:
        row = _target_row(target, dry_run=dry_run, force=force)
        rows.append(row)
        if row["action"] == "blocked":
            blocked_count += 1
            continue
        if dry_run:
            continue

        _remove_path(Path(target["target"]), allowed_root=Path(target["allowed_root"]))
        removed_count += 1
        if not target["media_only"]:
            latest_update = _repair_latest_after_run_delete(Path(target["run_dir"]))
            if latest_update is not None:
                latest_updates.append(latest_update)

    return {
        "schema_version": "4.0-clean",
        "dry_run": dry_run,
        "targets": rows,
        "removed_count": removed_count,
        "blocked_count": blocked_count,
        "latest_updates": latest_updates,
    }


def _target_for_run(
    run_dir: Path,
    *,
    media_only: bool,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    run_dir = _validate_run_dir(run_dir, storage_root=storage_root)
    status = inspect_run(run_dir)
    target = run_dir / "media" if media_only else run_dir
    if media_only and not target.exists():
        raise FileNotFoundError(target)
    allowed_root = run_dir if media_only else run_dir.parent
    return {
        "run_dir": str(run_dir),
        "target": str(target),
        "allowed_root": str(allowed_root),
        "media_only": media_only,
        "status": status,
        "size_bytes": _directory_size(target) if target.exists() else 0,
    }


def _target_row(target: dict[str, Any], *, dry_run: bool, force: bool) -> dict[str, Any]:
    status = target["status"]
    action = "would_remove" if dry_run else "removed"
    blocked_reason = None
    if status["status"] == "running" and not force:
        action = "blocked"
        blocked_reason = "run is marked running; stop it first or use --force"
    return {
        "run_dir": target["run_dir"],
        "target": target["target"],
        "action": action,
        "blocked_reason": blocked_reason,
        "status": status["status"],
        "stage": status["stage"],
        "size_bytes": target["size_bytes"],
        "size": _format_bytes(target["size_bytes"]),
    }


def _find_incomplete_runs(storage_dir: Path) -> list[Path]:
    runs_root = storage_dir / "items"
    if not runs_root.exists():
        return []
    runs: list[Path] = []
    for run_dir in runs_root.glob("*/runs/*"):
        if not run_dir.is_dir():
            continue
        if not any((run_dir / marker).exists() for marker in RUN_MARKERS):
            continue
        try:
            status = inspect_run(run_dir)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            continue
        if status["status"] != "completed":
            runs.append(run_dir)
    return sorted(runs)


def _validate_run_dir(run_dir: Path, *, storage_root: Path | None) -> Path:
    resolved = run_dir.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise FileNotFoundError(run_dir)
    if storage_root is not None:
        _ensure_within(resolved, storage_root.resolve())
    if resolved.parent.name != "runs":
        raise ValueError(f"refusing to clean non-run directory: {run_dir}")
    if not any((resolved / marker).exists() for marker in RUN_MARKERS):
        raise ValueError(f"refusing to clean directory without run markers: {run_dir}")
    return resolved


def _remove_path(path: Path, *, allowed_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = allowed_root.resolve()
    _ensure_within(resolved_path, resolved_root)
    if resolved_path == resolved_root and resolved_path.parent.name != "runs":
        raise ValueError(f"refusing to remove unsafe path: {path}")
    shutil.rmtree(resolved_path)


def _repair_latest_after_run_delete(run_dir: Path) -> dict[str, Any] | None:
    item_dir = run_dir.parent.parent
    latest_path = item_dir / "latest.json"
    if not latest_path.exists():
        return None
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if latest.get("run_id") != run_dir.name:
        return None

    replacement = _latest_completed_run(item_dir / "runs")
    if replacement is None:
        latest_path.unlink()
        return {"action": "deleted", "latest_path": str(latest_path)}

    latest_path.write_text(
        json.dumps(
            {
                "schema_version": "4.0-latest",
                "item_id": item_dir.name,
                "run_id": replacement.name,
                "run_dir": str(replacement.relative_to(item_dir)).replace("\\", "/"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "action": "repointed",
        "latest_path": str(latest_path),
        "run_id": replacement.name,
    }


def _latest_completed_run(runs_dir: Path) -> Path | None:
    candidates = [
        run_dir
        for run_dir in runs_dir.iterdir()
        if run_dir.is_dir()
        and (run_dir / "run.json").exists()
        and all((run_dir / artifact).exists() for artifact in FINAL_ARTIFACTS)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _directory_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def _ensure_within(path: Path, root: Path) -> None:
    if path == root:
        return
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing path outside allowed root: {path}") from exc


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")
