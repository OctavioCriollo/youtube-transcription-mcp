from __future__ import annotations

import argparse
import json
from pathlib import Path

from transcription_v4 import __version__
from transcription_v4.audit import audit_run, write_audit_files
from transcription_v4.clean import clean_incomplete_runs, clean_run, render_clean_text
from transcription_v4.pipeline import DEFAULT_PROFILE, MODEL_PROFILES, transcribe_file
from transcription_v4.pipeline import transcribe_youtube
from transcription_v4.providers import ELEVENLABS_PROVIDER, GROQ_PROVIDER, LOCAL_PROVIDER
from transcription_v4.plan import plan_file, render_plan_json, render_plan_text
from transcription_v4.repair import regenerate_run_outputs
from transcription_v4.status import inspect_run, render_status_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="transcribe-v4")
    parser.add_argument("--version", action="version", version=f"transcribe-v4 {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    file_parser = sub.add_parser("file", help="Transcribe a local audio/video file.")
    file_parser.add_argument("path", type=Path)
    file_parser.add_argument("--storage-dir", type=Path, default=Path("storage"))
    file_parser.add_argument(
        "--provider",
        choices=[LOCAL_PROVIDER, ELEVENLABS_PROVIDER, GROQ_PROVIDER],
        default=LOCAL_PROVIDER,
    )
    file_parser.add_argument(
        "--profile",
        choices=sorted(MODEL_PROFILES),
        default=DEFAULT_PROFILE,
        help="Model preset. Ignored when --model is provided.",
    )
    file_parser.add_argument("--model", default=None)
    file_parser.add_argument("--device", default="auto")
    file_parser.add_argument("--compute-type", default="auto")
    file_parser.add_argument(
        "--language",
        default=None,
        help="Optional language override. Omit for provider auto-detection.",
    )
    file_parser.add_argument("--allow-estimated-subtitles", action="store_true")
    file_parser.add_argument("--diarize", action="store_true")
    file_parser.add_argument("--num-speakers", type=int, default=None)
    file_parser.add_argument(
        "--tag-audio-events",
        dest="tag_audio_events",
        action="store_true",
        default=True,
    )
    file_parser.add_argument(
        "--no-tag-audio-events",
        dest="tag_audio_events",
        action="store_false",
    )
    file_parser.add_argument("--provider-timeout-s", type=float, default=3600.0)
    file_parser.add_argument(
        "--remote-audio-format",
        choices=["m4a", "mp3", "wav"],
        default="m4a",
    )
    file_parser.add_argument("--remote-audio-bitrate-kbps", type=int, default=128)
    file_parser.add_argument(
        "--chunk-duration-s",
        default="auto",
        help="Chunk size in seconds, 'auto' (default), or 'off'.",
    )
    file_parser.add_argument(
        "--no-chunking",
        action="store_true",
        help="Disable chunking even for long media.",
    )
    file_parser.add_argument("--overlap-s", type=float, default=2.0)
    file_parser.add_argument("--no-resume", action="store_true")
    file_parser.add_argument("--quiet", action="store_true")

    plan_parser = sub.add_parser("plan", help="Preview a local transcription run.")
    plan_parser.add_argument("path", type=Path)
    plan_parser.add_argument("--storage-dir", type=Path, default=Path("storage"))
    plan_parser.add_argument(
        "--provider",
        choices=[LOCAL_PROVIDER, ELEVENLABS_PROVIDER, GROQ_PROVIDER],
        default=LOCAL_PROVIDER,
    )
    plan_parser.add_argument(
        "--profile",
        choices=sorted(MODEL_PROFILES),
        default=DEFAULT_PROFILE,
        help="Model preset. Ignored when --model is provided.",
    )
    plan_parser.add_argument("--model", default=None)
    plan_parser.add_argument("--device", default="auto")
    plan_parser.add_argument("--compute-type", default="auto")
    plan_parser.add_argument(
        "--language",
        default=None,
        help="Optional language override. Omit for provider auto-detection.",
    )
    plan_parser.add_argument("--allow-estimated-subtitles", action="store_true")
    plan_parser.add_argument(
        "--chunk-duration-s",
        default="auto",
        help="Chunk size in seconds, 'auto' (default), or 'off'.",
    )
    plan_parser.add_argument(
        "--no-chunking",
        action="store_true",
        help="Disable chunking even for long media.",
    )
    plan_parser.add_argument("--overlap-s", type=float, default=2.0)
    plan_parser.add_argument("--no-resume", action="store_true")
    plan_parser.add_argument("--quiet", action="store_true")
    plan_parser.add_argument("--json", action="store_true")

    youtube_parser = sub.add_parser("youtube", help="Transcribe a YouTube URL.")
    youtube_parser.add_argument("url")
    youtube_parser.add_argument("--storage-dir", type=Path, default=Path("storage"))
    youtube_parser.add_argument(
        "--provider",
        choices=[LOCAL_PROVIDER, ELEVENLABS_PROVIDER, GROQ_PROVIDER],
        default=ELEVENLABS_PROVIDER,
    )
    youtube_parser.add_argument(
        "--profile",
        choices=sorted(MODEL_PROFILES),
        default=DEFAULT_PROFILE,
        help="Local model preset. Ignored when --model is provided.",
    )
    youtube_parser.add_argument("--model", default=None)
    youtube_parser.add_argument("--device", default="auto")
    youtube_parser.add_argument("--compute-type", default="auto")
    youtube_parser.add_argument(
        "--language",
        default=None,
        help="Optional language override. Omit for provider auto-detection.",
    )
    youtube_parser.add_argument("--allow-estimated-subtitles", action="store_true")
    youtube_parser.add_argument("--diarize", action="store_true")
    youtube_parser.add_argument("--num-speakers", type=int, default=None)
    youtube_parser.add_argument(
        "--tag-audio-events",
        dest="tag_audio_events",
        action="store_true",
        default=True,
    )
    youtube_parser.add_argument(
        "--no-tag-audio-events",
        dest="tag_audio_events",
        action="store_false",
    )
    youtube_parser.add_argument("--provider-timeout-s", type=float, default=3600.0)
    youtube_parser.add_argument(
        "--chunk-duration-s",
        default="auto",
        help="Local chunk size in seconds, 'auto' (default), or 'off'.",
    )
    youtube_parser.add_argument(
        "--no-chunking",
        action="store_true",
        help="Disable local chunking even for long media.",
    )
    youtube_parser.add_argument("--overlap-s", type=float, default=2.0)
    youtube_parser.add_argument("--no-resume", action="store_true")
    youtube_parser.add_argument("--quiet", action="store_true")

    audit_parser = sub.add_parser("audit", help="Audit an existing transcription run.")
    audit_parser.add_argument("run_dir", type=Path)
    audit_parser.add_argument("--no-write", action="store_true")

    repair_parser = sub.add_parser(
        "repair",
        help="Regenerate TXT/SRT/VTT/quality/audit from an existing canonical.json.",
    )
    repair_parser.add_argument("run_dir", type=Path)
    repair_parser.add_argument("--allow-estimated-subtitles", action="store_true")

    status_parser = sub.add_parser("status", help="Inspect progress for an existing run.")
    status_parser.add_argument("run_dir", type=Path)
    status_parser.add_argument("--json", action="store_true")

    clean_parser = sub.add_parser("clean", help="Clean transcription run artifacts safely.")
    clean_parser.add_argument("run_dir", nargs="?", type=Path)
    clean_parser.add_argument("--storage-dir", type=Path, default=Path("storage"))
    clean_parser.add_argument(
        "--incomplete",
        action="store_true",
        help="Clean all incomplete runs under --storage-dir.",
    )
    clean_parser.add_argument(
        "--media-only",
        action="store_true",
        help="Remove only media/prepared.wav and media/chunks for target runs.",
    )
    clean_parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply cleanup. Without this flag, clean only prints a dry-run preview.",
    )
    clean_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow cleaning runs marked running. Stop active processes first.",
    )
    clean_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "file":
        run_dir = transcribe_file(
            args.path,
            storage_dir=args.storage_dir,
            provider=args.provider,
            model=args.model,
            profile=args.profile,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
            allow_estimated_subtitles=args.allow_estimated_subtitles,
            chunk_duration_s="off" if args.no_chunking else args.chunk_duration_s,
            overlap_s=args.overlap_s,
            resume=not args.no_resume,
            progress=not args.quiet,
            diarize=args.diarize,
            num_speakers=args.num_speakers,
            tag_audio_events=args.tag_audio_events,
            provider_timeout_s=args.provider_timeout_s,
            remote_audio_format=args.remote_audio_format,
            remote_audio_bitrate_kbps=args.remote_audio_bitrate_kbps,
        )
        print(run_dir)
        return 0
    if args.command == "youtube":
        run_dir = transcribe_youtube(
            args.url,
            storage_dir=args.storage_dir,
            provider=args.provider,
            profile=args.profile,
            model=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
            allow_estimated_subtitles=args.allow_estimated_subtitles,
            chunk_duration_s="off" if args.no_chunking else args.chunk_duration_s,
            overlap_s=args.overlap_s,
            resume=not args.no_resume,
            progress=not args.quiet,
            diarize=args.diarize,
            num_speakers=args.num_speakers,
            tag_audio_events=args.tag_audio_events,
            provider_timeout_s=args.provider_timeout_s,
        )
        print(run_dir)
        return 0
    if args.command == "plan":
        report = plan_file(
            args.path,
            storage_dir=args.storage_dir,
            provider=args.provider,
            model=args.model,
            profile=args.profile,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
            allow_estimated_subtitles=args.allow_estimated_subtitles,
            chunk_duration_s="off" if args.no_chunking else args.chunk_duration_s,
            overlap_s=args.overlap_s,
            resume=not args.no_resume,
            progress=(not args.quiet and not args.json),
        )
        if args.json:
            print(render_plan_json(report))
        else:
            print(render_plan_text(report), end="")
        return 0
    if args.command == "audit":
        report = audit_run(args.run_dir)
        if not args.no_write:
            write_audit_files(args.run_dir, report)
        summary = report["summary"]
        print(f"{summary['status']}: {summary['verdict']}")
        if not args.no_write:
            print(args.run_dir / "audit.json")
            print(args.run_dir / "audit.txt")
        return 0
    if args.command == "repair":
        result = regenerate_run_outputs(
            args.run_dir,
            allow_estimated_subtitles=args.allow_estimated_subtitles or None,
        )
        print(
            "quality={quality_status} audit={audit_status} cues={cue_count}".format(
                **result
            )
        )
        print(args.run_dir)
        return 0
    if args.command == "status":
        report = inspect_run(args.run_dir)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_status_text(report), end="")
        return 0
    if args.command == "clean":
        if args.incomplete:
            if args.run_dir is not None:
                raise SystemExit("clean accepts either RUN_DIR or --incomplete, not both")
            report = clean_incomplete_runs(
                args.storage_dir,
                dry_run=not args.yes,
                media_only=args.media_only,
                force=args.force,
            )
        else:
            if args.run_dir is None:
                raise SystemExit("clean requires RUN_DIR or --incomplete")
            report = clean_run(
                args.run_dir,
                dry_run=not args.yes,
                media_only=args.media_only,
                force=args.force,
            )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_clean_text(report), end="")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
