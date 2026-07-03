from __future__ import annotations

import os
from collections.abc import Callable, Sequence
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
    def __init__(
        self,
        *,
        cookies_file: Path | None = None,
        proxy: str | None = None,
        player_clients: Sequence[str] | None = None,
        pot_provider_url: str | None = None,
    ) -> None:
        self.cookies_file = Path(cookies_file).expanduser().resolve() if cookies_file else None
        self.proxy = proxy.strip() if proxy else None
        # Datacenter-IP hardening. Both fall back to the environment so every
        # construction site (MCP-configured or bare default in the engine
        # pipeline) picks them up without threading extra parameters through
        # each layer. Explicit constructor arguments still win, for tests and
        # programmatic use.
        #   YT_PLAYER_CLIENTS   comma-separated yt-dlp player clients to try
        #                       (e.g. "tv,web_safari,mweb"). Rotating clients
        #                       sidesteps per-client blocks and PO-token
        #                       requirements that differ between clients.
        #   YT_POT_PROVIDER_URL base URL of a bgutil-ytdlp-pot-provider
        #                       sidecar (e.g. "http://bgutil-pot:4416").
        #                       Requires the bgutil-ytdlp-pot-provider plugin
        #                       to be installed (done in the Docker image).
        if player_clients is None:
            raw = os.environ.get("YT_PLAYER_CLIENTS", "")
            parsed = tuple(part.strip() for part in raw.split(",") if part.strip())
            player_clients = parsed or None
        if pot_provider_url is None:
            pot_provider_url = os.environ.get("YT_POT_PROVIDER_URL", "").strip() or None
        self.player_clients = tuple(player_clients) if player_clients else None
        self.pot_provider_url = pot_provider_url

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
        if self.cookies_file is not None:
            if not self.cookies_file.is_file():
                raise YoutubeDownloadError(f"yt-dlp cookies file does not exist: {self.cookies_file}")
            options["cookiefile"] = str(self.cookies_file)
        if self.proxy:
            options["proxy"] = self.proxy
        extractor_args: dict[str, dict[str, list[str]]] = {}
        if self.player_clients:
            extractor_args["youtube"] = {"player_client": list(self.player_clients)}
        if self.pot_provider_url:
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [self.pot_provider_url]}
        if extractor_args:
            options["extractor_args"] = extractor_args
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
