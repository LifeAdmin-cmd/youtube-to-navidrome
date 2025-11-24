import json
import os
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

        # Store session data
        self.tracks = {}

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise OperationCancelled("Cancelled by user.")

    def cleanup(self):
        """Removes temporary files on cancellation."""
        print("[System] Cleaning up...")
        time.sleep(1)
        for ext in ["*.part", "*.ytdl", "*.f*", "*.temp"]:
            for f in self.output_dir.glob(ext):
                try:
                    f.unlink()
                except:
                    pass

    def delete_track(self, track_uid: str):
        """Deletes a track from disk and session."""
        if track_uid not in self.tracks:
            raise ValueError("Track not found.")

        track = self.tracks[track_uid]
        path = track.get("path")

        if path and path.exists():
            try:
                path.unlink()
            except Exception as e:
                raise IOError(f"Failed to delete file: {e}")

        # Mark as deleted in session or remove
        del self.tracks[track_uid]
        return True

    def _download_and_process(self, url: str, info: Dict) -> Path:
        """Helper to download, sponsorblock, and cut a video."""
        # 1. Download
        raw_file = self.downloader.download_video(url, info)

        # 2. SponsorBlock
        vid_id = info["id"]
        duration = info["duration"]

        # Now allows exceptions to bubble up to be caught by _process_video
        bad_segments = self.sponsorblock.get_segments(
            vid_id, ["music_offtopic", "intro", "outro", "selfpromo", "interaction"]
        )
        good_segments = self.sponsorblock.invert_segments(duration, bad_segments)

        # 3. Cutting
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

        return final_file

    def rerun_spotify_search(self, track_uid: str, custom_query: str = None) -> Dict:
        """Performs a new Spotify search, updates the candidates list, and returns results."""
        if track_uid not in self.tracks:
            raise ValueError("Track not found.")

        track = self.tracks[track_uid]

        # Treat empty string as None
        if custom_query == "":
            custom_query = None

        if custom_query:
            # If custom query provided, search with just the query, no uploader
            search_title = custom_query
            search_uploader = None
        else:
            # If no custom query, use original YouTube title and uploader
            search_title = track["youtube_title"]
            search_uploader = track["video_info"].get("uploader")

        print(
            f"[Rerun Search] Searching for: {search_title} (Uploader: {search_uploader})"
        )

        # Perform the search
        search_res = self.spotify.search_tracks(search_title, search_uploader)

        # Update the candidates list in the track data (important for get_candidates endpoint and for UI consistency)
        track["candidates"] = search_res["candidates"]

        # The frontend will want to display the search string that was used.
        query_display = (
            f"{search_title} - {search_uploader}" if search_uploader else search_title
        )

        return {"query_used": query_display, "candidates": search_res["candidates"]}

    def update_track_tags(self, track_uid: str, spotify_id: str) -> Dict:
        """Retags an existing file or processes a skipped file with new metadata."""
        if track_uid not in self.tracks:
            raise ValueError("Track not found in history.")

        track_data = self.tracks[track_uid]

        # Fetch new metadata
        new_meta = self.spotify.get_track_metadata(spotify_id)
        tags = new_meta["tags"]

        if self.processor.check_duplicate(
            tags["Title"], tags["Artist"], tags.get("Album", "")
        ):
            raise ValueError(
                f"Track '{tags['Title']} - {tags['Artist']}' already exists in the database."
            )

        if track_data.get("path") and track_data["path"].exists():
            current_path = track_data["path"]
            new_path = self.processor.tag_audio(current_path, tags)
        else:
            print(f"[Update] Downloading skipped track: {track_data['youtube_title']}")
            untagged_file = self._download_and_process(
                track_data["url"], track_data["video_info"]
            )
            new_path = self.processor.tag_audio(untagged_file, tags)

        self.tracks[track_uid]["path"] = new_path
        self.tracks[track_uid]["status"] = "success"

        return {
            "new_filename": new_path.name,
            "spotify_title": tags["Title"],
            "spotify_artist": tags["Artist"],
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

            self.tracks[track_uid] = {
                "video_info": info,
                "url": url,
                "candidates": [],
                "youtube_title": title,
                "path": None,
                "status": "pending",
            }

            yield json.dumps(
                {
                    "status": "processing",
                    "message": f"Processing: {title}",
                    "progress": base_progress,
                }
            )

            # 2. Spotify Search
            spotify_result = None
            candidates = []
            tags = None

            try:
                search_res = self.spotify.search_tracks(title, uploader)
                spotify_result = search_res["best_match"]
                candidates = search_res["candidates"]
                self.tracks[track_uid]["candidates"] = candidates
            except Exception as e:
                # Log API error and re-raise or handle gracefully
                # We yield the error here so the user sees it
                yield json.dumps(
                    {
                        "status": "error",
                        "message": f"Spotify API Error for '{title}': {str(e)}",
                        "progress": base_progress,
                    }
                )
                # If Search fails completely, we treat it as no tags found
                spotify_result = None

            # --- Check if tags were found ---
            if not spotify_result:
                self.tracks[track_uid]["status"] = "error"
                yield json.dumps(
                    {
                        "status": "error",
                        "message": f"No tags found for '{title}'. File not saved.",
                        "progress": base_progress,
                        "track_uid": track_uid,
                        "youtube_title": title,
                        "spotify_title": None,
                        "spotify_artist": None,
                    }
                )
                return  # Stop processing this file
            # --------------------------------

            tags = spotify_result["tags"]

            # 3. Duplicate Check
            if self.processor.check_duplicate(
                tags["Title"], tags["Artist"], tags.get("Album", "")
            ):
                self.tracks[track_uid]["status"] = "skipped"
                yield json.dumps(
                    {
                        "status": "skipped",
                        "message": f"Skipped: {tags['Title']} exists.",
                        "progress": base_progress,
                        "track_uid": track_uid,
                        "youtube_title": title,
                        "spotify_title": tags["Title"],
                        "spotify_artist": tags["Artist"],
                    }
                )
                return

            # 4. Download & Process
            yield json.dumps(
                {
                    "status": "processing",
                    "message": "Downloading & Processing...",
                    "progress": base_progress,
                }
            )

            final_file = self._download_and_process(url, info)

            # 5. Tagging
            yield json.dumps(
                {
                    "status": "processing",
                    "message": "Applying tags...",
                    "progress": base_progress,
                }
            )
            final_file = self.processor.tag_audio(final_file, tags)

            self.tracks[track_uid]["path"] = final_file
            self.tracks[track_uid]["status"] = "success"

            yield json.dumps(
                {
                    "status": "success",
                    "message": f"Saved: {final_file.name}",
                    "track_uid": track_uid,
                    "youtube_title": title,
                    "spotify_title": tags["Title"],
                    "spotify_artist": tags["Artist"],
                    "progress": base_progress,
                }
            )

        except Exception as e:
            if isinstance(e, OperationCancelled):
                raise e

            # Log the specific error to UI
            yield json.dumps(
                {
                    "status": "error",
                    "message": f"Error processing {url}: {str(e)}",
                    "progress": base_progress,
                    "track_uid": track_uid,
                    "youtube_title": title if "title" in locals() else "Unknown",
                    "spotify_title": None,
                    "spotify_artist": None,
                }
            )
