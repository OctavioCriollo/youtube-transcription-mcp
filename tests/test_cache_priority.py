"""Corrective 4: the cache selects by provider PRIORITY (not recency), and never
serves the degraded subtitles fallback."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

FINAL = (
    "canonical.json",
    "transcript.txt",
    "transcript-timestamps.txt",
    "subtitles.srt",
    "subtitles.vtt",
    "quality.json",
    "audit.json",
    "audit.txt",
)


def _write_complete_run(runs_dir, run_id, *, provider, url, mtime=None):
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    for name in FINAL:
        if name == "canonical.json":
            (run_dir / name).write_text(
                json.dumps(
                    {"language": "en", "duration": 3.0, "model": "m", "provider": provider}
                ),
                encoding="utf-8",
            )
        elif name == "transcript.txt":
            (run_dir / name).write_text(f"hello from {provider}", encoding="utf-8")
        elif name == "audit.json":
            (run_dir / name).write_text(
                json.dumps({"summary": {"status": "pass", "verdict": "ok"}}), encoding="utf-8"
            )
        elif name == "quality.json":
            (run_dir / name).write_text(json.dumps({"status": "pass"}), encoding="utf-8")
        else:
            (run_dir / name).write_text("ok", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "metadata": {
                    "source_type": "youtube",
                    "source_url": url,
                    "provider": provider,
                    "transcription_provider": provider,
                    "requested_language": None,
                    "diarize": False,
                    "num_speakers": None,
                },
                "artifacts": {"transcript_txt": "transcript.txt"},
            }
        ),
        encoding="utf-8",
    )
    if mtime is not None:
        for path in run_dir.rglob("*"):
            os.utime(path, (mtime, mtime))
        os.utime(run_dir, (mtime, mtime))
    return run_dir


def test_cache_prefers_higher_priority_over_recency(tmp_path):
    from transcription_mcp.pipeline import _read_cached_url_result
    from transcription_engine.storage import item_id_for_url

    url = "https://youtu.be/example"
    runs = tmp_path / "storage" / "items" / item_id_for_url(url) / "runs"
    now = datetime.now(UTC).timestamp()
    # groq is OLDER, elevenlabs is NEWER. Priority must beat recency.
    _write_complete_run(runs, "run_groq", provider="groq", url=url, mtime=now - 1000)
    _write_complete_run(runs, "run_eleven", provider="elevenlabs", url=url, mtime=now - 50)

    result = _read_cached_url_result(
        url=url,
        workspace_dir=tmp_path,
        provider_order=("groq", "elevenlabs", "subtitles"),
        language=None,
        diarize=False,
        num_speakers=None,
        cache_ttl_hours=24,
    )

    assert result is not None
    assert result["method"] == "groq"  # higher priority wins over the newer elevenlabs run
    assert result["cache"]["hit"] is True


def test_cache_never_serves_subtitles(tmp_path):
    from transcription_mcp.pipeline import _read_cached_url_result
    from transcription_engine.storage import item_id_for_url

    url = "https://youtu.be/subsonly"
    runs = tmp_path / "storage" / "items" / item_id_for_url(url) / "runs"
    _write_complete_run(runs, "run_subs", provider="subtitles", url=url)

    result = _read_cached_url_result(
        url=url,
        workspace_dir=tmp_path,
        provider_order=("groq", "elevenlabs", "subtitles"),
        language=None,
        diarize=False,
        num_speakers=None,
        cache_ttl_hours=24,
    )

    # A cached subtitles run must never be reused (it would shadow groq/elevenlabs).
    assert result is None
