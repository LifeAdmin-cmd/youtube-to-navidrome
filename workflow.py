import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterator, List

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
        system_cores = os.cpu_count() or 4
        env_workers = os.getenv("MAX_WORKERS")
        if env_workers:
            try:
                self.MAX_WORKERS = int(env_workers)
            except ValueError:
                self.MAX_WORKERS = system_cores
        else:
            self.MAX_WORKERS = system_cores
        print(f"[System] Parallel workers set to: {self.MAX_WORKERS}")

        # Thread safety
        self.state_lock = threading.RLock()

        # --- NEW: Broadcasting & State ---
        self.listeners: List[queue.Queue] = []
        self.is_active = False
        self.current_progress = 0

        # Ensure output directory exists before attempting to load state
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Load tracks AND logs
        loaded_state = self._load_state()
        self.tracks = loaded_state.get("tracks", {})
        self.logs = loaded_state.get("logs", [])

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

            # Save complete state including logs
            full_state = {
                "tracks": serializable_tracks,
                "logs": self.logs[-500:],  # Keep last 500 logs to avoid huge files
            }

            try:
                with state_path.open("w") as f:
                    json.dump(full_state, f, indent=4)
            except Exception as e:
                print(f"[System] Warning: Failed to save state: {e}")

    def _load_state(self) -> Dict[str, Any]:
        state_path = self._get_state_path()
        if not state_path.exists():
            return {"tracks": {}, "logs": []}

        try:
            with state_path.open("r") as f:
                data = json.load(f)

            # Backwards compatibility if file was just tracks dict
            if "tracks" not in data and "logs" not in data and data:
                # Assume old format (just tracks)
                tracks_data = data
                logs_data = []
            else:
                tracks_data = data.get("tracks", {})
                logs_data = data.get("logs", [])

            for track in tracks_data.values():
                if track.get("path") and isinstance(track["path"], str):
                    track["path"] = Path(track["path"])

            return {"tracks": tracks_data, "logs": logs_data}
        except Exception as e:
            print(f"[System] Load error: {e}")
            return {"tracks": {}, "logs": []}

    # --- NEW: Broadcasting System ---

    def _broadcast(self, message_data: Dict):
        """Sends a message to all active listeners and saves to log."""
        msg_str = json.dumps(message_data)

        with self.state_lock:
            # Update internal state
            if "progress" in message_data:
                self.current_progress = message_data["progress"]

            # Add to history (only messages with text)
            if message_data.get("message"):
                # Add timestamp/style if needed, or just store the raw dict
                self.logs.append(message_data)
                # Auto-save occasionally? For now, we save on specific events or exit.

        # Push to all active queues
        dead_listeners = []
        for q in self.listeners:
            try:
                q.put_nowait(msg_str)
            except queue.Full:
                dead_listeners.append(q)

        # Cleanup full/dead queues
        for q in dead_listeners:
            if q in self.listeners:
                self.listeners.remove(q)

    def subscribe(self) -> Iterator[str]:
        """Yields events for a new client."""
        q = queue.Queue()
        self.listeners.append(q)
        try:
            # Yield active state immediately so UI syncs up
            yield json.dumps(
                {
                    "status": "info",
                    "message": "Connected to stream.",
                    "progress": self.current_progress,
                }
            )

            while True:
                msg = q.get()
                yield f"data: {msg}\n\n"
        except GeneratorExit:
            if q in self.listeners:
                self.listeners.remove(q)

    def get_full_state(self):
        """Returns the complete data needed to restore the UI."""
        with self.state_lock:
            return {
                "tracks": self.tracks,  # Flask jsonify handles dicts
                "logs": self.logs,
                "is_active": self.is_active,
                "current_progress": self.current_progress,
            }

    def start_processing(self, url: str):
        """Starts the processing loop in a background thread."""
        if self.is_active:
            raise Exception("A task is already running.")

        self.cancel_event.clear()
        self.is_active = True

        # --- CLEARS THE QUEUE ---
        with self.state_lock:
            self.logs = []
            self.tracks = {}  # Explicitly clear tracks from the previous run
            self._save_state()

        thread = threading.Thread(target=self._run_background_task, args=(url,))
        thread.daemon = True
        thread.start()

    def start_retry(self):
        """Starts the retry loop in a background thread."""
        if self.is_active:
            raise Exception("A task is already running.")

        self.cancel_event.clear()
        self.is_active = True

        thread = threading.Thread(target=self._run_retry_task)
        thread.daemon = True
        thread.start()

    # --- Internal Background Tasks ---

    def _run_background_task(self, url: str):
        self._broadcast(
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
                self._broadcast(
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
                if self.cancel_event.is_set():
                    break

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

            # Consumption Loop
            finished_count = 0
            while finished_count < len(futures):
                if self.cancel_event.is_set():
                    raise OperationCancelled("Cancelled")

                try:
                    # Fetch from workers and broadcast
                    msg = msg_queue.get(timeout=0.5)
                    # Parse to dict to pass to _broadcast
                    if isinstance(msg, str):
                        msg = json.loads(msg)

                    current_prog = int((finished_count / total) * 100)
                    if msg.get("progress") == 0:
                        msg["progress"] = current_prog

                    self._broadcast(msg)
                except queue.Empty:
                    pass

                finished_count = sum(1 for f in futures if f.done())

            # Flush
            while not msg_queue.empty():
                msg = msg_queue.get()
                if isinstance(msg, str):
                    msg = json.loads(msg)
                self._broadcast(msg)

            self._broadcast(
                {
                    "status": "finished",
                    "message": "All operations completed!",
                    "progress": 100,
                }
            )

        except OperationCancelled:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
            self.cleanup()
            self._broadcast(
                {
                    "status": "cancelled",
                    "message": "Operation cancelled.",
                    "progress": 0,
                }
            )

        except Exception as e:
            self._broadcast({"status": "fatal_error", "message": str(e), "progress": 0})

        finally:
            if executor:
                executor.shutdown(wait=True)
            self.is_active = False
            self._save_state()

    def _run_retry_task(self):
        with self.state_lock:
            failed_uids = [
                uid for uid, track in self.tracks.items() if track["status"] == "error"
            ]
        total = len(failed_uids)

        if total == 0:
            self.is_active = False
            self._broadcast(
                {"status": "finished", "message": "No failed tracks.", "progress": 100}
            )
            return

        self._broadcast(
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
                if self.cancel_event.is_set():
                    break
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
                if self.cancel_event.is_set():
                    raise OperationCancelled("Cancelled")
                try:
                    msg = msg_queue.get(timeout=0.5)
                    if isinstance(msg, str):
                        msg = json.loads(msg)
                    current_prog = int((finished_count / total) * 100)
                    msg["progress"] = current_prog
                    self._broadcast(msg)
                except queue.Empty:
                    pass
                finished_count = sum(1 for f in futures if f.done())

            while not msg_queue.empty():
                msg = msg_queue.get()
                if isinstance(msg, str):
                    msg = json.loads(msg)
                self._broadcast(msg)

            self._broadcast({"status": "finished", "message": "Done!", "progress": 100})

        except OperationCancelled:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
            self.cleanup()
            self._broadcast(
                {
                    "status": "cancelled",
                    "message": "Operation cancelled.",
                    "progress": 0,
                }
            )

        finally:
            if executor:
                executor.shutdown(wait=True)
            self.is_active = False
            self._save_state()

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise OperationCancelled("Cancelled by user.")

    def cleanup(self):
        print("[System] Cleaning up...")
        time.sleep(2)
        extensions = ["*.part", "*.ytdl", "*.f*", "*.temp", "*.webp"]
        for attempt in range(3):
            found = 0
            for ext in extensions:
                for f in self.output_dir.glob(ext):
                    found += 1
                    try:
                        f.unlink()
                    except:
                        pass
            if found == 0:
                break
            time.sleep(1)

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
                raise IOError(f"Failed to delete: {e}")
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
                print(f"Error deleting {uid}: {e}")
        return count

    def _download_and_process(self, url: str, info: Dict) -> Path:
        raw_file = self.downloader.download_video(url, info)
        vid_id = info["id"]
        duration = info["duration"]
        bad_segments = self.sponsorblock.get_segments(
            vid_id, ["music_offtopic", "intro", "outro", "selfpromo", "interaction"]
        )
        good_segments = self.sponsorblock.invert_segments(duration, bad_segments)

        self.check_cancel()

        # Change: Always create a 'processed' file to apply normalization
        processed_file = raw_file.with_name(f"{raw_file.stem}_processed.opus")

        # If good_segments represents the whole file, it just normalizes.
        # If it contains specific segments, it cuts AND normalizes.
        self.processor.cut_audio(raw_file, processed_file, good_segments)

        # Clean up the raw download and rename processed file
        raw_file.unlink()
        processed_file.rename(raw_file)

        return raw_file

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
                raise ValueError("Track not found.")
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
            self.tracks[track_uid]["match_score"] = 1.0  # Manual selection = 100%
            self._save_state()
        return {
            "new_filename": new_path.name,
            "spotify_title": tags["Title"],
            "spotify_artist": tags["Artist"],
            "spotify_cover": tags.get("Album Cover Art"),
            "match_score": 1.0,
        }

    def _worker_wrapper(self, func, msg_queue: queue.Queue, *args, **kwargs):
        if self.cancel_event.is_set():
            return
        try:
            for item in func(*args, **kwargs):
                if self.cancel_event.is_set():
                    break
                msg_queue.put(item)
        except OperationCancelled:
            return
        except Exception as e:
            if "Cancelled by user" in str(e):
                return
            msg_queue.put(
                json.dumps({"status": "error", "message": f"Thread crashed: {e}"})
            )

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
                    "match_score": 0.0,
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
            match_score = 0.0
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
                        # The modified Spotify client returns the score in the metadata
                        match_score = spotify_result.get("score", 0.0)
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
                self.tracks[track_uid]["match_score"] = match_score

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
                        "spotify_cover": tags.get("Album Cover Art"),
                        "match_score": match_score,
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
                    "spotify_cover": tags.get("Album Cover Art"),
                    "match_score": match_score,
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
                }
            )
