from __future__ import annotations

import json
from datetime import UTC, datetime


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_parse_provider_order_defaults_and_rejects_subtitles_for_media():
    import pytest

    from transcription_mcp.pipeline import parse_provider_order

    assert parse_provider_order(None, allow_subtitles=True) == (
        "groq",
        "elevenlabs",
        "subtitles",
    )
    assert parse_provider_order("local, groq", allow_subtitles=False) == ("local", "groq")
    with pytest.raises(ValueError, match="subtitles"):
        parse_provider_order("groq,subtitles", allow_subtitles=False)


def test_diarized_youtube_request_skips_non_diarization_providers(tmp_path):
    import pytest

    from transcription_mcp.pipeline import TranscriptionFailed, transcribe_youtube_sync

    with pytest.raises(TranscriptionFailed) as exc:
        transcribe_youtube_sync(
            url="https://youtu.be/example",
            language=None,
            workspace_dir=tmp_path,
            provider_order="groq,subtitles",
            diarize=True,
        )

    assert "does not support diarization" in str(exc.value)


def test_read_run_artifacts_returns_manifest_and_speakers(tmp_path):
    from transcription_mcp.pipeline import _read_run_artifacts

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "canonical.json",
        {
            "language": "es",
            "duration": 3.0,
            "model": "whisper-large-v3-turbo",
            "provider": "groq",
        },
    )
    _write_json(
        run_dir / "run.json",
        {
            "metadata": {
                "source_type": "youtube",
                "source_url": "https://youtu.be/example",
                "youtube_video_id": "example",
                "transcription_provider": "groq",
            },
            "artifacts": {
                "transcript_txt": "transcript.txt",
                "subtitles_srt": "subtitles.srt",
            },
        },
    )
    _write_json(run_dir / "audit.json", {"summary": {"status": "pass", "verdict": "ok"}})
    _write_json(run_dir / "quality.json", {"status": "pass"})
    _write_json(run_dir / "speakers.json", {"total_speakers": 1})
    (run_dir / "transcript.txt").write_text("hola", encoding="utf-8")
    (run_dir / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhola", encoding="utf-8")

    result = _read_run_artifacts(run_dir)

    assert result["source"]["url"] == "https://youtu.be/example"
    assert result["artifacts"]["subtitles_srt"]["exists"] is True
    assert result["artifacts"]["run_json"]["exists"] is True
    assert result["speakers"]["total_speakers"] == 1


def test_cached_completed_url_result_is_returned(tmp_path):
    from transcription_mcp.pipeline import _read_cached_url_result
    from transcription_engine.storage import item_id_for_url

    url = "https://youtu.be/example"
    run_dir = tmp_path / "storage" / "items" / item_id_for_url(url) / "runs" / "run_cached"
    run_dir.mkdir(parents=True)
    for name in (
        "transcript-timestamps.txt",
        "subtitles.srt",
        "subtitles.vtt",
        "audit.txt",
    ):
        (run_dir / name).write_text("ok", encoding="utf-8")
    (run_dir / "transcript.txt").write_text("hola cache", encoding="utf-8")
    _write_json(
        run_dir / "canonical.json",
        {
            "language": "es",
            "duration": 3.0,
            "model": "whisper-large-v3-turbo",
            "provider": "groq",
        },
    )
    _write_json(run_dir / "audit.json", {"summary": {"status": "pass", "verdict": "ok"}})
    _write_json(run_dir / "quality.json", {"status": "pass"})
    _write_json(
        run_dir / "run.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": {
                "source_type": "youtube",
                "source_url": url,
                "provider": "groq",
                "transcription_provider": "groq",
                "requested_language": None,
                "diarize": False,
                "num_speakers": None,
            },
            "artifacts": {"transcript_txt": "transcript.txt"},
        },
    )

    result = _read_cached_url_result(
        url=url,
        workspace_dir=tmp_path,
        provider_order=("groq", "elevenlabs"),
        language=None,
        diarize=False,
        num_speakers=None,
        cache_ttl_hours=24,
    )

    assert result is not None
    assert result["transcript"] == "hola cache"
    assert result["method"] == "groq"
    assert result["cache"]["hit"] is True
    assert result["provider_order_effective"] == ["groq", "elevenlabs"]
