import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterator

import yt_dlp as ytdlp

from services.downloader import Downloader
from services.processor import AudioProcessor
from services.sponsorblock import SponsorBlockClient
from services.spotify import SpotifyClient
from utils import OperationCancelled


class WorkflowManager:
    """Co-ordinates the entire downloading and processing workflow."""

    def __init__(self, output_dir: str = None):
        env_dir = output_dir or os.getenv("output_directory")
        self.output_dir = Path(env_dir) if env_dir else Path(".")
        self.cancel_event = threading.Event()

        self.spotify = SpotifyClient()
        self.sponsorblock = SponsorBlockClient()
        self.downloader = Downloader(self.output_dir, self.cancel_event)
        self.processor = AudioProcessor()

        # Store session data: { track_uid: { 'path': Path, 'candidates': [], 'youtube_title': str } }
        self.tracks = {}

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise OperationCancelled("Cancelled by user.")

    def cleanup(self):
        """Removes temporary files on cancellation."""
        print("[System] Cleaning up...")
        time.sleep(1)

        # 1. Clean yt-dlp temps
        for ext in ["*.part", "*.ytdl", "*.f*", "*.temp"]:
            for f in self.output_dir.glob(ext):
                try:
                    f.unlink()
                except:
                    pass

        # 2. Clean incomplete opus files
        for f in self.output_dir.glob("*.opus"):
            if (
                re.search(r"\[[a-zA-Z0-9_-]{11}\]", f.name)
                or "_cut" in f.name
                or len(f.stem) == 11
            ):
                try:
                    f.unlink()
                    print(f"[Cleanup] Deleted: {f.name}")
                except:
                    pass

    def update_track_tags(self, track_uid: str, spotify_id: str) -> Dict:
        """Retags an existing file with new Spotify metadata."""
        if track_uid not in self.tracks:
            raise ValueError("Track not found in history.")

        track_data = self.tracks[track_uid]
        current_path = track_data["path"]

        if not current_path.exists():
            raise FileNotFoundError(f"File {current_path} not found.")

        # Fetch new metadata
        new_meta = self.spotify.get_track_metadata(spotify_id)

        # Retag
        new_path = self.processor.tag_audio(current_path, new_meta["tags"])

        # Update state
        self.tracks[track_uid]["path"] = new_path

        return {
            "new_filename": new_path.name,
            "spotify_title": new_meta["tags"]["Title"],
            "spotify_artist": new_meta["tags"]["Artist"],
        }

    def process_url(self, url: str) -> Iterator[str]:
        self.cancel_event.clear()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        yield json.dumps(
            {"status": "processing", "message": "Analyzing URL...", "progress": 0}
        )

        try:
            with ytdlp.YoutubeDL({"extract_flat": True, "quiet": True}) as ydl:
                info = ydl.extract_info(url, download=False)

            entries = info.get("entries") if "entries" in info else [info]
            entries = [e for e in entries if e]
            total = len(entries)

            if "entries" in info:
                yield json.dumps(
                    {
                        "status": "processing",
                        "message": f"Playlist detected. {total} items.",
                        "progress": 0,
                    }
                )

            for i, entry in enumerate(entries):
                self.check_cancel()
                video_url = (
                    entry.get("url")
                    or f"https://www.youtube.com/watch?v={entry.get('id')}"
                )
                progress = int((i / total) * 100)

                for update in self._process_video(video_url, entry, progress):
                    self.check_cancel()
                    yield update

            yield json.dumps(
                {
                    "status": "finished",
                    "message": "All operations completed!",
                    "progress": 100,
                }
            )

        except OperationCancelled:
            self.cleanup()
            yield json.dumps({"status": "cancelled", "message": "Operation cancelled."})
        except Exception as e:
            yield json.dumps({"status": "fatal_error", "message": str(e)})

    def _process_video(
        self, url: str, pre_info: Dict, base_progress: int
    ) -> Iterator[str]:
        track_uid = str(uuid.uuid4())

        try:
            if not pre_info.get("duration"):
                with ytdlp.YoutubeDL({"quiet": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
            else:
                info = pre_info

            title = info.get("title")
            uploader = info.get("uploader")
            vid_id = info["id"]
            duration = info["duration"]

            yield json.dumps(
                {
                    "status": "processing",
                    "message": f"Processing: {title}",
                    "progress": base_progress,
                }
            )

            # Spotify Search
            spotify_result = None
            candidates = []
            tags = None

            try:
                search_res = self.spotify.search_tracks(title, uploader)
                spotify_result = search_res["best_match"]
                candidates = search_res["candidates"]

                if spotify_result:
                    tags = spotify_result["tags"]
                    if self.processor.check_duplicate(
                        self.output_dir, tags["Title"], tags.get("Album", "")
                    ):
                        yield json.dumps(
                            {
                                "status": "skipped",
                                "message": f"Skipped: {tags['Title']} exists.",
                                "progress": base_progress,
                            }
                        )
                        return
            except Exception as e:
                print(f"Metadata error: {e}")

            # Download
            yield json.dumps(
                {
                    "status": "processing",
                    "message": "Downloading...",
                    "progress": base_progress,
                }
            )
            raw_file = self.downloader.download_video(url, info)

            # SponsorBlock
            yield json.dumps(
                {
                    "status": "processing",
                    "message": "Checking cuts...",
                    "progress": base_progress,
                }
            )
            bad_segments = self.sponsorblock.get_segments(
                vid_id, ["music_offtopic", "intro", "outro", "selfpromo", "interaction"]
            )
            good_segments = self.sponsorblock.invert_segments(duration, bad_segments)

            # Cutting
            final_file = raw_file
            if not (
                len(good_segments) == 1
                and good_segments[0][0] == 0
                and good_segments[0][1] == duration
            ):
                cut_file = raw_file.with_name(f"{raw_file.stem}_cut.opus")
                self.processor.cut_audio(raw_file, cut_file, good_segments)
                raw_file.unlink()
                cut_file.rename(raw_file)
                final_file = raw_file

            # Tagging
            if spotify_result:
                yield json.dumps(
                    {
                        "status": "processing",
                        "message": "Applying tags...",
                        "progress": base_progress,
                    }
                )
                final_file = self.processor.tag_audio(final_file, tags)

            # Save to session history
            self.tracks[track_uid] = {
                "path": final_file,
                "youtube_title": title,
                "candidates": candidates,
            }

            yield json.dumps(
                {
                    "status": "success",
                    "message": f"Saved: {final_file.name}",
                    "track_uid": track_uid,
                    "youtube_title": title,
                    "spotify_title": tags["Title"] if tags else "No Match Found",
                    "progress": base_progress,
                }
            )

        except Exception as e:
            if isinstance(e, OperationCancelled):
                raise e
            yield json.dumps(
                {
                    "status": "error",
                    "message": f"Error processing {url}: {e}",
                    "progress": base_progress,
                }
            )
