import threading
from pathlib import Path
from typing import Any, Dict

import yt_dlp as ytdlp

from utils import OperationCancelled, Utils


class Downloader:
    """Wrapper for yt-dlp with cancellation support."""

    def __init__(self, output_dir: Path, cancel_event: threading.Event):
        self.output_dir = output_dir
        self.cancel_event = cancel_event

    def _progress_hook(self, d):
        if self.cancel_event.is_set():
            raise OperationCancelled("Download aborted by user.")

    def download_video(self, url: str, video_info: Dict[str, Any]) -> Path:
        vid_id = video_info["id"]
        title_safe = Utils.sanitize_filename(video_info["title"])

        filename = f"{title_safe} [{vid_id}].opus"
        out_file = self.output_dir / filename

        ydl_opts = {
            "outtmpl": str(self.output_dir / "%(title)s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "format": "bestaudio/best",
            "overwrites": True,
            "progress_hooks": [self._progress_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "opus",
                    "preferredquality": "192",
                }
            ],
        }

        with ytdlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not out_file.exists():
            candidates = list(self.output_dir.glob(f"*{vid_id}*.opus"))
            if candidates:
                return candidates[0]
            raise FileNotFoundError("Downloaded file not found.")

        return out_file
