from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from transcription_v4.chunking import ChunkInfo, plan_chunks

MediaProgressCallback = Callable[[str], None]


class FfmpegNotFoundError(RuntimeError):
    pass


def _resolve_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if sys.platform.startswith("win"):
        candidate = Path("C:/ffmpeg/bin") / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    for candidate in (Path("/usr/bin") / name, Path("/usr/local/bin") / name):
        if candidate.exists():
            return str(candidate)
    raise FfmpegNotFoundError(f"{name} not found in PATH")


class FfmpegMedia:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        self.ffmpeg = ffmpeg or _resolve_binary("ffmpeg")
        self.ffprobe = ffprobe or _resolve_binary("ffprobe")

    def extract_audio(
        self,
        src: Path,
        dst: Path,
        *,
        sample_rate: int = 16000,
        progress_callback: MediaProgressCallback | None = None,
    ) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._run_with_file_progress(
            [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                str(dst),
            ],
            monitor_path=dst,
            progress_callback=progress_callback,
            progress_label="[media] prepared.wav",
        )

    def extract_remote_audio(
        self,
        src: Path,
        dst: Path,
        *,
        audio_format: str = "m4a",
        bitrate_kbps: int = 128,
        sample_rate: int = 16000,
        progress_callback: MediaProgressCallback | None = None,
    ) -> None:
        audio_format = audio_format.lower().strip()
        if audio_format not in {"m4a", "mp3", "wav"}:
            raise ValueError("remote audio format must be m4a, mp3, or wav")
        if bitrate_kbps <= 0:
            raise ValueError("bitrate_kbps must be > 0")

        dst.parent.mkdir(parents=True, exist_ok=True)
        if audio_format == "wav":
            codec_args = ["-acodec", "pcm_s16le"]
        elif audio_format == "mp3":
            codec_args = ["-acodec", "libmp3lame", "-b:a", f"{bitrate_kbps}k"]
        else:
            codec_args = ["-acodec", "aac", "-b:a", f"{bitrate_kbps}k"]

        self._run_with_file_progress(
            [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vn",
                *codec_args,
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                str(dst),
            ],
            monitor_path=dst,
            progress_callback=progress_callback,
            progress_label=f"[media] remote-input.{audio_format}",
        )

    def get_duration(self, path: Path) -> float:
        out = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture=True,
        )
        return float(out.strip())

    def split_audio(
        self,
        src: Path,
        dst_dir: Path,
        *,
        chunk_duration_s: float,
        overlap_s: float = 2.0,
        progress_callback: MediaProgressCallback | None = None,
    ) -> list[ChunkInfo]:
        duration = self.get_duration(src)
        dst_dir.mkdir(parents=True, exist_ok=True)
        planned = plan_chunks(
            duration_s=duration,
            chunk_duration_s=chunk_duration_s,
            overlap_s=overlap_s,
        )
        chunks: list[ChunkInfo] = []
        total_chunks = len(planned)
        for position, info in enumerate(planned, start=1):
            dst = dst_dir / f"chunk_{info.index:04d}.wav"
            if progress_callback is not None:
                progress_callback(
                    f"[chunking] creating chunk {position}/{total_chunks} "
                    f"{info.nominal_start:.1f}s-{info.nominal_end:.1f}s"
                )
            self._run_with_file_progress(
                [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(src),
                    "-ss",
                    f"{info.actual_start:.3f}",
                    "-to",
                    f"{info.nominal_end:.3f}",
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    str(dst),
                ],
                monitor_path=dst,
                progress_callback=progress_callback,
                progress_label=f"[chunking] chunk {position}/{total_chunks}",
            )
            if progress_callback is not None:
                progress_callback(
                    f"[chunking] chunk {position}/{total_chunks} ready "
                    f"({_format_bytes(dst.stat().st_size)})"
                )
            chunks.append(
                ChunkInfo(
                    index=info.index,
                    nominal_start=info.nominal_start,
                    nominal_end=info.nominal_end,
                    actual_start=info.actual_start,
                    overlap_left=info.overlap_left,
                    path=dst,
                )
            )
        return chunks

    @staticmethod
    def _run(cmd: list[str], *, capture: bool = False) -> str:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg command failed with exit {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout if capture else ""

    @staticmethod
    def _run_with_file_progress(
        cmd: list[str],
        *,
        monitor_path: Path,
        progress_callback: MediaProgressCallback | None,
        progress_label: str,
        progress_interval_s: float = 10.0,
    ) -> None:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        last_report_at = time.monotonic()
        last_reported_size = -1

        while process.poll() is None:
            time.sleep(0.5)
            if progress_callback is None:
                continue
            now = time.monotonic()
            if now - last_report_at < progress_interval_s:
                continue
            size = monitor_path.stat().st_size if monitor_path.exists() else 0
            if size > 0:
                progress_callback(f"{progress_label} {_format_bytes(size)} written")
            else:
                progress_callback(f"{progress_label} working")
            last_report_at = now
            last_reported_size = size

        stdout, stderr = process.communicate()
        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip()
            raise RuntimeError(
                f"ffmpeg command failed with exit {process.returncode}: {detail}"
            )
        if progress_callback is not None and monitor_path.exists():
            final_size = monitor_path.stat().st_size
            if final_size != last_reported_size:
                progress_callback(f"{progress_label} {_format_bytes(final_size)} written")


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")
