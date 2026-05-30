from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class YtDlpNotInstalledError(RuntimeError):
    pass


class YoutubeDownloadError(RuntimeError):
    pass


YoutubeProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class YoutubeDownloadResult:
    audio_path: Path
    metadata: dict[str, Any]


class YtDlpYoutubeDownloader:
    def download_audio(
        self,
        url: str,
        output_dir: Path,
        *,
        progress_callback: YoutubeProgressCallback | None = None,
    ) -> YoutubeDownloadResult:
        try:
            import yt_dlp  # type: ignore[import-not-found]
            from yt_dlp.utils import DownloadError, ExtractorError  # type: ignore[import-not-found]
        except ImportError as exc:
            raise YtDlpNotInstalledError(
                "yt-dlp is not installed. Install with: pip install -e ."
            ) from exc

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(output_dir / "source.%(ext)s")

        options = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_progress_hook(progress_callback)],
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                sanitized = ydl.sanitize_info(info)
        except (DownloadError, ExtractorError) as exc:
            raise YoutubeDownloadError(f"yt-dlp failed to download audio: {exc}") from exc
        except Exception as exc:
            raise YoutubeDownloadError(f"yt-dlp failed to download audio: {exc}") from exc

        audio_path = _downloaded_audio_path(output_dir, sanitized)
        if progress_callback is not None:
            progress_callback(f"[youtube] downloaded audio {audio_path.name}")
        return YoutubeDownloadResult(
            audio_path=audio_path,
            metadata=_metadata_from_info(sanitized),
        )


def ensure_yt_dlp_installed() -> None:
    try:
        import yt_dlp  # noqa: F401  # type: ignore[import-not-found]
    except ImportError as exc:
        raise YtDlpNotInstalledError(
            "yt-dlp is not installed. Install with: pip install -e ."
        ) from exc


def _progress_hook(callback: YoutubeProgressCallback | None):
    def hook(data: dict[str, Any]) -> None:
        if callback is None:
            return
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            if total:
                pct = (downloaded / total) * 100
                callback(f"[youtube] downloading {pct:.1f}%")
            elif downloaded:
                callback(f"[youtube] downloaded {_format_bytes(int(downloaded))}")
        elif status == "finished":
            callback("[youtube] download finished")

    return hook


def _downloaded_audio_path(output_dir: Path, info: dict[str, Any]) -> Path:
    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict) and item.get("filepath"):
                path = Path(str(item["filepath"]))
                if path.exists():
                    return path

    unfinished_suffixes = {".part", ".tmp", ".ytdl"}
    candidates = sorted(
        (
            path
            for path in output_dir.glob("source.*")
            if path.is_file() and path.suffix not in unfinished_suffixes
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise YoutubeDownloadError(f"yt-dlp did not create source audio in {output_dir}")


def _metadata_from_info(info: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "title",
        "duration",
        "channel",
        "channel_id",
        "uploader",
        "uploader_id",
        "webpage_url",
        "extractor",
        "extractor_key",
        "upload_date",
    ]
    return {key: info.get(key) for key in keys if info.get(key) is not None}


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")
