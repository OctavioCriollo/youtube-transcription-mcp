"""Corrective 5a: the subtitles path produces a real run (full artifact set),
so create_transcription_bundle and get_transcription_artifact work for it too."""

from __future__ import annotations


def _fake_captions() -> dict:
    return {
        "transcript": "hello world this is a caption test",
        "language": "en",
        "duration_s": 6.0,
        "model": "youtube-captions",
        "provider": "youtube-transcript-api",
        "estimated_cost_usd": 0.0,
        "segments": [
            {"start": 0.0, "end": 2.5, "text": "hello world"},
            {"start": 2.5, "end": 6.0, "text": "this is a caption test"},
        ],
        "timestamp_level": "caption",
        "word_timestamps": False,
        "source_timestamps": "youtube_captions",
        "youtube": {"video_id": "vid12345678", "title": "T", "channel": "C"},
        "method": "subtitles",
    }


def test_build_subtitles_run_writes_full_artifact_set(tmp_path):
    from transcription_mcp.pipeline import _build_subtitles_run, _read_run_artifacts

    run_dir = _build_subtitles_run(
        url="https://youtu.be/vid12345678",
        language=None,
        captions=_fake_captions(),
        workspace_dir=tmp_path,
    )

    for name in (
        "transcript.txt",
        "transcript-timestamps.txt",
        "subtitles.srt",
        "subtitles.vtt",
        "canonical.json",
        "quality.json",
        "audit.json",
        "run.json",
    ):
        assert (run_dir / name).is_file(), f"missing {name}"

    result = _read_run_artifacts(run_dir)
    assert result["run_dir"] == str(run_dir)
    assert result["artifacts"]["subtitles_srt"]["exists"] is True
    # Caption content must NOT be a hard failure; warning is acceptable.
    assert result["quality_status"] != "error"
    assert "hello world" in result["transcript"]


def test_subtitles_fallback_returns_run_dir_for_delivery(tmp_path, monkeypatch):
    from transcription_mcp import pipeline

    monkeypatch.setattr(
        pipeline,
        "fetch_subtitles_transcript",
        lambda url, language=None: _fake_captions(),
    )

    result = pipeline._try_subtitles_fallback(
        url="https://youtu.be/vid12345678",
        language=None,
        workspace_dir=tmp_path,
        failed_attempts={"groq": "blocked", "elevenlabs": "auth"},
        provider_order=("groq", "elevenlabs", "subtitles"),
        status_callback=None,
    )

    assert result is not None
    assert result["method"] == "subtitles"
    assert result["run_dir"]
    # The delivery tools need run_dir + an artifact manifest; this is the fix.
    assert result["artifacts"]["transcript_txt"]["exists"] is True
    assert result["artifacts"]["subtitles_srt"]["exists"] is True
    assert result["word_timestamps"] is False
    assert result["timestamp_level"] == "caption"
    assert result["provider_order_effective"] == ["groq", "elevenlabs", "subtitles"]
    assert result["failed_attempts"] == {"groq": "blocked", "elevenlabs": "auth"}
