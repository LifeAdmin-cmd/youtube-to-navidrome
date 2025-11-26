import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterator

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
        self.MAX_SEARCH_ATTEMPTS = 3

        # --- Worker Count Logic ---
        # 1. Default to CPU count (or 4 if detection fails)
        system_cores = os.cpu_count() or 4

        # 2. Check for environment override
        env_workers = os.getenv("MAX_WORKERS")

        if env_workers:
            try:
                self.MAX_WORKERS = int(env_workers)
            except ValueError:
                print(
                    f"[System] Warning: Invalid MAX_WORKERS '{env_workers}'. Using system default: {system_cores}"
                )
                self.MAX_WORKERS = system_cores
        else:
            self.MAX_WORKERS = system_cores

        print(f"[System] Parallel workers set to: {self.MAX_WORKERS}")

        # Thread safety for state saving
        self.state_lock = threading.RLock()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tracks = self._load_state()

        self.spotify = SpotifyClient()
        self.sponsorblock = SponsorBlockClient()
        self.downloader = Downloader(self.output_dir, self.cancel_event)
        self.processor = AudioProcessor()

    def _get_state_path(self) -> Path:
        return self.output_dir / "workflow_state.json"

    def _save_state(self):
        state_path = self._get_state_path()
        with self.state_lock:
            serializable_tracks = {}
            for uid, track in self.tracks.items():
                serializable_track = track.copy()
                if serializable_track.get("path"):
                    serializable_track["path"] = str(serializable_track["path"])
                serializable_tracks[uid] = serializable_track

            try:
                with state_path.open("w") as f:
                    json.dump(serializable_tracks, f, indent=4)
            except Exception as e:
                print(f"[System] Warning: Failed to save state: {e}")

    def _load_state(self) -> Dict[str, Any]:
        state_path = self._get_state_path()
        if not state_path.exists():
            return {}
        try:
            with state_path.open("r") as f:
                loaded_tracks = json.load(f)
            for track in loaded_tracks.values():
                if track.get("path") and isinstance(track["path"], str):
                    track["path"] = Path(track["path"])
            return loaded_tracks
        except Exception:
            return {}

    def check_cancel(self):
        """Checks if the cancel flag is set and raises an exception if so."""
        if self.cancel_event.is_set():
            raise OperationCancelled("Cancelled by user.")

    def cleanup(self):
        """Robust cleanup that retries on Windows file locks."""
        print("[System] Cleaning up...")

        extensions = ["*.part", "*.ytdl", "*.f*", "*.temp", "*.webp"]

        # Try to delete 3 times with a delay to allow file handles to close
        for attempt in range(3):
            files_found = 0
            for ext in extensions:
                for f in self.output_dir.glob(ext):
                    files_found += 1
                    try:
                        f.unlink()
                    except Exception:
                        pass  # Ignore errors, will retry

            if files_found == 0:
                break  # Done

            time.sleep(1.0)  # Wait for OS/Antivirus to release files

    def delete_track(self, track_uid: str):
        with self.state_lock:
            if track_uid not in self.tracks:
                raise ValueError("Track not found.")
            track = self.tracks[track_uid]
            path = track.get("path")

        if path and path.exists():
            try:
                path.unlink()
            except Exception as e:
                raise IOError(f"Failed to delete file: {e}")

        with self.state_lock:
            if track_uid in self.tracks:
                del self.tracks[track_uid]
            self._save_state()
        return True

    def delete_all_failed_tracks(self) -> int:
        with self.state_lock:
            failed_uids = [
                uid for uid, track in self.tracks.items() if track["status"] == "error"
            ]
        count = 0
        for uid in failed_uids:
            try:
                self.delete_track(uid)
                count += 1
            except Exception as e:
                print(f"[System] Error deleting failed track {uid}: {e}")
        return count

    def _download_and_process(self, url: str, info: Dict) -> Path:
        raw_file = self.downloader.download_video(url, info)
        vid_id = info["id"]
        duration = info["duration"]

        bad_segments = self.sponsorblock.get_segments(
            vid_id, ["music_offtopic", "intro", "outro", "selfpromo", "interaction"]
        )
        good_segments = self.sponsorblock.invert_segments(duration, bad_segments)

        final_file = raw_file
        if not (
            len(good_segments) == 1
            and good_segments[0][0] == 0
            and good_segments[0][1] == duration
        ):
            self.check_cancel()
            cut_file = raw_file.with_name(f"{raw_file.stem}_cut.opus")
            self.processor.cut_audio(raw_file, cut_file, good_segments)
            raw_file.unlink()
            cut_file.rename(raw_file)
            final_file = raw_file

        return final_file

    def rerun_spotify_search(self, track_uid: str, custom_query: str = None) -> Dict:
        with self.state_lock:
            if track_uid not in self.tracks:
                raise ValueError("Track not found.")
            track = self.tracks[track_uid]

        self.cancel_event.clear()

        search_title = track["youtube_title"]
        if custom_query:
            query_to_use = custom_query
            query_display = custom_query
        else:
            uploader = track["video_info"].get("uploader")
            query_to_use = f"{search_title} - {uploader}" if uploader else search_title
            query_display = query_to_use

        search_res = self.spotify.search_raw(query_to_use, original_title=search_title)

        with self.state_lock:
            self.tracks[track_uid]["candidates"] = search_res["candidates"]
            self._save_state()

        return {"query_used": query_display, "candidates": search_res["candidates"]}

    def update_track_tags(self, track_uid: str, spotify_id: str) -> Dict:
        with self.state_lock:
            if track_uid not in self.tracks:
                raise ValueError("Track not found in history.")
            track_data = self.tracks[track_uid]

        self.cancel_event.clear()

        new_meta = self.spotify.get_track_metadata(spotify_id)
        tags = new_meta["tags"]

        if self.processor.check_duplicate(
            tags["Title"], tags["Artist"], tags.get("Album", "")
        ):
            raise ValueError(
                f"Track '{tags['Title']} - {tags['Artist']}' already exists."
            )

        if track_data.get("path") and track_data["path"].exists():
            new_path = self.processor.tag_audio(track_data["path"], tags)
        else:
            untagged_file = self._download_and_process(
                track_data["url"], track_data["video_info"]
            )
            new_path = self.processor.tag_audio(untagged_file, tags)

        with self.state_lock:
            self.tracks[track_uid]["path"] = new_path
            self.tracks[track_uid]["status"] = "success"
            self.tracks[track_uid]["best_match_tags"] = tags
            self._save_state()

        return {
            "new_filename": new_path.name,
            "spotify_title": tags["Title"],
            "spotify_artist": tags["Artist"],
        }

    def _worker_wrapper(self, func, msg_queue: queue.Queue, *args, **kwargs):
        """Runs a task and puts result in queue. Exits silently if cancelled."""
        if self.cancel_event.is_set():
            return

        try:
            for item in func(*args, **kwargs):
                if self.cancel_event.is_set():
                    break
                msg_queue.put(item)

        # Explicitly catch the custom cancellation exception
        except OperationCancelled:
            return  # Exit silently, this is expected behavior

        except Exception as e:
            # Also catch specific string message if exception type matching fails
            if "aborted by user" in str(e) or "Cancelled by user" in str(e):
                return

            msg_queue.put(
                json.dumps({"status": "error", "message": f"Thread crashed: {e}"})
            )

    def process_url(self, url: str) -> Iterator[str]:
        self.cancel_event.clear()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        yield json.dumps(
            {"status": "processing", "message": "Analyzing URL...", "progress": 0}
        )

        executor = None
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

            msg_queue = queue.Queue()

            executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS)
            futures = []

            for i, entry in enumerate(entries):
                self.check_cancel()
                video_url = (
                    entry.get("url")
                    or f"https://www.youtube.com/watch?v={entry.get('id')}"
                )

                future = executor.submit(
                    self._worker_wrapper,
                    self._process_video,
                    msg_queue,
                    video_url,
                    entry,
                    0,
                )
                futures.append(future)

            finished_count = 0
            while finished_count < len(futures):
                self.check_cancel()

                try:
                    msg = msg_queue.get(timeout=0.5)
                    yield msg
                except queue.Empty:
                    pass

                finished_count = sum(1 for f in futures if f.done())

            while not msg_queue.empty():
                yield msg_queue.get()

            yield json.dumps(
                {
                    "status": "finished",
                    "message": "All operations completed!",
                    "progress": 100,
                }
            )

        except OperationCancelled:
            if executor:
                # Cancel futures stops new tasks
                # Wait=True waits for current tasks to finish/abort (releasing file handles)
                executor.shutdown(wait=True, cancel_futures=True)

            # Now safe to cleanup
            self.cleanup()
            yield json.dumps({"status": "cancelled", "message": "Operation cancelled."})

        except Exception as e:
            yield json.dumps({"status": "fatal_error", "message": str(e)})

        finally:
            if executor:
                executor.shutdown(wait=True)

    def retry_failed_tracks(self) -> Iterator[str]:
        self.cancel_event.clear()

        with self.state_lock:
            failed_uids = [
                uid for uid, track in self.tracks.items() if track["status"] == "error"
            ]

        total = len(failed_uids)
        if total == 0:
            yield json.dumps(
                {"status": "finished", "message": "No failed tracks.", "progress": 100}
            )
            return

        yield json.dumps(
            {
                "status": "processing",
                "message": f"Retrying {total} tracks...",
                "progress": 0,
            }
        )

        msg_queue = queue.Queue()
        executor = None

        try:
            executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS)
            futures = []

            for i, uid in enumerate(failed_uids):
                self.check_cancel()
                future = executor.submit(
                    self._worker_wrapper,
                    self._process_track_execution,
                    msg_queue,
                    uid,
                    0,
                )
                futures.append(future)

            finished_count = 0
            while finished_count < total:
                self.check_cancel()
                try:
                    msg = msg_queue.get(timeout=0.5)
                    yield msg
                except queue.Empty:
                    pass
                finished_count = sum(1 for f in futures if f.done())

            while not msg_queue.empty():
                yield msg_queue.get()

            yield json.dumps(
                {"status": "finished", "message": "Done!", "progress": 100}
            )

        except OperationCancelled:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
            self.cleanup()
            yield json.dumps({"status": "cancelled", "message": "Operation cancelled."})

        finally:
            if executor:
                executor.shutdown(wait=True)

    def _process_video(
        self, url: str, pre_info: Dict, base_progress: int
    ) -> Iterator[str]:
        self.check_cancel()

        track_uid = str(uuid.uuid4())
        title = pre_info.get("title", "Unknown")

        try:
            if not pre_info.get("duration"):
                with ytdlp.YoutubeDL({"quiet": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
            else:
                info = pre_info

            title = info.get("title", "Unknown")

            with self.state_lock:
                self.tracks[track_uid] = {
                    "video_info": info,
                    "url": url,
                    "candidates": [],
                    "youtube_title": title,
                    "path": None,
                    "status": "pending",
                    "best_match_tags": None,
                }
                self._save_state()

            yield json.dumps(
                {
                    "status": "processing",
                    "message": f"Processing: {title}",
                    "progress": base_progress,
                }
            )

            yield from self._process_track_execution(track_uid, base_progress)

        except Exception as e:
            if isinstance(e, OperationCancelled):
                raise e
            with self.state_lock:
                if track_uid in self.tracks:
                    self.tracks[track_uid]["status"] = "error"
                    self._save_state()
            yield json.dumps(
                {
                    "status": "error",
                    "message": f"Error: {str(e)}",
                    "progress": base_progress,
                    "track_uid": track_uid,
                    "youtube_title": title,
                    "spotify_title": None,
                    "spotify_artist": None,
                }
            )

    def _process_track_execution(
        self, track_uid: str, base_progress: int
    ) -> Iterator[str]:
        self.check_cancel()

        with self.state_lock:
            track = self.tracks[track_uid]

        title = track["youtube_title"]
        uploader = track["video_info"].get("uploader")
        url = track["url"]
        info = track["video_info"]

        try:
            spotify_result = None
            candidates = []
            tags = None
            last_error = None

            for attempt in range(1, self.MAX_SEARCH_ATTEMPTS + 1):
                self.check_cancel()
                try:
                    search_res = self.spotify.search_tracks(
                        title, uploader, attempt=attempt
                    )
                    if search_res["best_match"]:
                        spotify_result = search_res["best_match"]
                        tags = spotify_result["tags"]
                        candidates = search_res["candidates"]
                        break
                    candidates = search_res["candidates"]
                except Exception as e:
                    last_error = f"Spotify Error: {str(e)}"
                    yield json.dumps(
                        {
                            "status": "warning",
                            "message": last_error,
                            "progress": base_progress,
                        }
                    )

            with self.state_lock:
                self.tracks[track_uid]["candidates"] = candidates
                self.tracks[track_uid]["best_match_tags"] = tags

            if not spotify_result:
                with self.state_lock:
                    self.tracks[track_uid]["status"] = "error"
                    self._save_state()
                yield json.dumps(
                    {
                        "status": "error",
                        "message": last_error or "No tags found.",
                        "progress": base_progress,
                        "track_uid": track_uid,
                        "youtube_title": title,
                        "spotify_title": None,
                        "spotify_artist": None,
                    }
                )
                return

            if self.processor.check_duplicate(
                tags["Title"], tags["Artist"], tags.get("Album", "")
            ):
                with self.state_lock:
                    self.tracks[track_uid]["status"] = "skipped"
                    self._save_state()
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

            yield json.dumps(
                {
                    "status": "processing",
                    "message": f"Downloading {title}...",
                    "progress": base_progress,
                }
            )

            self.check_cancel()

            final_file = self._download_and_process(url, info)

            final_file = self.processor.tag_audio(final_file, tags)

            with self.state_lock:
                self.tracks[track_uid]["path"] = final_file
                self.tracks[track_uid]["status"] = "success"
                self._save_state()

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
            with self.state_lock:
                self.tracks[track_uid]["status"] = "error"
                self._save_state()
            yield json.dumps(
                {
                    "status": "error",
                    "message": f"Error: {str(e)}",
                    "progress": base_progress,
                    "track_uid": track_uid,
                    "youtube_title": title,
                    "spotify_title": None,
                    "spotify_artist": None,
                }
            )
