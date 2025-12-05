import concurrent.futures
import os
import re
import threading
import time
from typing import Any, Dict, List

import requests


class MusicBrainzClient:
    """Handles MusicBrainz Search and Metadata Retrieval."""

    BASE_API = "https://musicbrainz.org/ws/2"
    COVER_ART_API = "https://coverartarchive.org"
    THEAUDIODB_API_BASE = "https://www.theaudiodb.com/api/v1/json"

    HEADERS = {
        "User-Agent": "YoutubeToNavidrome/1.0 ( generic_user@example.com )",
        "Accept": "application/json",
    }

    def __init__(self):
        self._lock = threading.Lock()  # <--- NEU: Lock initialisieren

    def _get(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Helper to make rate-limited requests to MusicBrainz."""
        url = f"{self.BASE_API}/{endpoint}"
        if params:
            params["fmt"] = "json"

        # <--- NEU: 'with self._lock:' Block hinzufügen
        # Dies zwingt alle Threads, nacheinander zu warten und anzufragen.
        with self._lock:
            time.sleep(1.1)

            try:
                resp = requests.get(
                    url, headers=self.HEADERS, params=params, timeout=15
                )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                print(f"[MusicBrainz] Request Error: {e}")
                raise e

    def _get_cover_from_theaudiodb(self, artist: str, title: str) -> str:
        """Fallback: Fetch cover art from TheAudioDB."""
        if not artist or not title or title == "Unknown Album":
            return None

        # Use configured API key or fallback to test key "2"
        api_key = os.getenv("THEAUDIODB_API_KEY", "2")
        url = f"{self.THEAUDIODB_API_BASE}/{api_key}/searchtrack.php"

        try:
            print(f"[TheAudioDB] Searching for cover: {artist} - {title}")
            resp = requests.get(url, params={"s": artist, "t": title}, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("track"):
                    for track in data["track"]:
                        thumb = track.get("strTrackThumb")
                        if thumb:
                            return thumb
        except Exception as e:
            print(f"[TheAudioDB] Error: {e}")

        return None

    def get_track_metadata(self, mbid: str) -> Dict[str, Any]:
        """Fetches metadata for a specific Recording ID (MBID)."""
        data = self._get(f"recording/{mbid}", params={"inc": "artists+releases+genres"})

        title = data.get("title")

        artist_credits = data.get("artist-credit", [])
        artist_names = [
            ac.get("name")
            for ac in artist_credits
            if isinstance(ac, dict) and "name" in ac
        ]
        if not artist_names and artist_credits:
            artist_names = [
                x["artist"]["name"] for x in artist_credits if "artist" in x
            ]

        releases = data.get("releases", [])
        best_release = None

        for rel in releases:
            status = rel.get("status", "").lower()
            if status == "official":
                best_release = rel
                break

        if not best_release and releases:
            best_release = releases[0]

        album_name = best_release.get("title") if best_release else "Unknown Album"
        release_id = best_release.get("id") if best_release else None
        date = best_release.get("date") if best_release else None

        cover_art = None

        # 1. Try Cover Art Archive
        if release_id:
            try:
                ca_url = f"{self.COVER_ART_API}/release/{release_id}"
                ca_resp = requests.get(ca_url, timeout=5)
                if ca_resp.ok:
                    images = ca_resp.json().get("images", [])
                    for img in images:
                        if "Front" in img.get("types", []) or img.get("front"):
                            cover_art = img.get("image")
                            break
                    if not cover_art and images:
                        cover_art = images[0].get("image")
            except Exception:
                print(f"[MusicBrainz] No cover art found for release {release_id}")

        genres = [t.get("name") for t in data.get("tags", [])]

        return {
            "tags": {
                "Album Cover Art": cover_art,
                "Album": album_name,
                "Artist": ", ".join(artist_names),
                "Artists": artist_names,
                "Title": title,
                "Genre": genres[0] if genres else None,
                "Label": None,
                "Release Date": date,
                "Description": f"MusicBrainz ID: {mbid}",
            }
        }

    def _word_match_ratio(self, text1: str, text2: str) -> float:
        words1 = set(re.findall(r"\w+", text1.lower()))
        words2 = set(re.findall(r"\w+", text2.lower()))
        intersection = words1.intersection(words2)
        total = len(words1) + len(words2)
        return (2.0 * len(intersection)) / total if total > 0 else 0.0

    def _resolve_cover_art(self, candidate_data: Dict[str, Any]) -> str:
        """
        Helper to check valid cover art for a candidate.
        1. Checks Cover Art Archive URL validity (HEAD request).
        2. Falls back to TheAudioDB.
        """
        rid = candidate_data.get("release_id")
        artist = candidate_data.get("artist")
        title = candidate_data.get("title")

        # 1. Check Optimistic CAA URL
        if rid:
            # /front-250 is the thumbnail; HEAD request verifies it exists
            optimistic_url = f"https://coverartarchive.org/release/{rid}/front-250"
            try:
                # 2 second timeout for existence check
                resp = requests.head(optimistic_url, timeout=2, allow_redirects=True)
                if resp.status_code == 200:
                    return optimistic_url
            except Exception:
                pass

        # 2. Fallback to TheAudioDB
        if artist and title and title != "Unknown":
            # Taking the first artist usually works best for TheAudioDB
            primary_artist = artist.split(",")[0].strip()
            return self._get_cover_from_theaudiodb(artist=primary_artist, title=title)

        return None

    def _score_candidates(
        self, recordings: List[Dict[str, Any]], target_title: str
    ) -> Dict[str, Any]:
        """Helper to score tracks and find the best match/candidates list."""

        candidates = []
        best_track = None
        best_score = -1.0

        # 1. Build initial candidate objects (lightweight)
        for rec in recordings:
            t_name = rec.get("title", "")

            artist_credits = rec.get("artist-credit", [])
            a_names_list = [
                x["artist"]["name"] for x in artist_credits if "artist" in x
            ]
            a_names = ", ".join(a_names_list)

            releases = rec.get("releases", [])
            album_name = "Unknown"
            rid = None

            if releases:
                # Try to find an official release, otherwise take the first one
                best_rel = next(
                    (r for r in releases if r.get("status") == "Official"), releases[0]
                )
                album_name = best_rel.get("title")
                rid = best_rel.get("id")

            full_str = f"{a_names} {t_name}"
            score = self._word_match_ratio(target_title, full_str)

            candidate = {
                "id": rec["id"],
                "title": t_name,
                "artist": a_names,
                "album": album_name,
                "release_id": rid,  # Stored for resolution
                "image": None,  # <--- Bleibt vorerst leer!
                "score": score,
            }
            candidates.append(candidate)

        # --- ENTFERNT: Der automatische Bilder-Download Block wurde hier gelöscht ---

        # 3. Sort and pick best
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Find best track (highest score)
        if candidates:
            top_candidate = candidates[0]
            if top_candidate["score"] > best_score:
                best_score = top_candidate["score"]
                best_track = top_candidate

        result = {"best_match": None, "candidates": candidates}

        # Falls wir einen direkten Treffer für die Tabelle haben, holen wir hier immer noch
        # die vollen Metadaten (inkl. Cover für diesen EINEN Treffer).
        if best_track and best_score > 0.4:
            print(
                f"[MusicBrainz] Match: '{best_track['title']}' (Score: {best_score:.2f})"
            )
            try:
                # Full fetch for the best match to ensure we get detailed tags
                meta = self.get_track_metadata(best_track["id"])
                artist_names = meta["tags"].get("Artists", [])
                title = meta["tags"].get("Title")

                # 2. Fallback to TheAudioDB
                if not meta["tags"].get("Album Cover Art") and artist_names:
                    cover_art = self._get_cover_from_theaudiodb(artist_names[0], title)
                if cover_art:
                    meta["tags"]["Album Cover Art"] = cover_art
                meta["score"] = best_score
                result["best_match"] = meta
            except Exception as e:
                print(f"[MusicBrainz] Failed to fetch metadata for best match: {e}")

        return result

    def fetch_candidate_covers(
        self, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Lädt die Cover-Bilder nachträglich.
        Diese Methode wird erst aufgerufen, wenn der User den Edit-Dialog öffnet.
        """
        # Wir laden nur die Top 10, um Ressourcen zu sparen
        subset = candidates[:10]

        if subset:
            print(
                f"[MusicBrainz] Fetching covers for {len(subset)} candidates on demand..."
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_cand = {
                    executor.submit(self._resolve_cover_art, c): c for c in subset
                }
                for future in concurrent.futures.as_completed(future_to_cand):
                    cand = future_to_cand[future]
                    try:
                        cand["image"] = future.result()
                    except Exception as e:
                        print(
                            f"[MusicBrainz] Image resolution failed for {cand['title']}: {e}"
                        )
        return candidates

    def _execute_search(self, query: str) -> List[Dict[str, Any]]:
        data = self._get("recording", params={"query": query, "limit": 10})
        return data.get("recordings", [])

    def search_tracks(
        self, title: str, uploader: str = None, market: str = "DE", attempt: int = 1
    ) -> Dict[str, Any]:
        clean_title = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", title).strip()
        q_str = ""

        if attempt == 1:
            if uploader and "topic" not in uploader.lower():
                q_str = f'recording:"{clean_title}" AND artist:"{uploader}"'
            else:
                q_str = f'recording:"{clean_title}"'
            print(f"[MusicBrainz] Attempt 1 Search: {q_str}")
        elif attempt == 2:
            q_str = f'recording:"{clean_title}"'
            print(f"[MusicBrainz] Attempt 2 Search: {q_str}")
        elif attempt == 3:
            q_str = clean_title
            print(f"[MusicBrainz] Attempt 3 Search: {q_str}")
        else:
            return {"best_match": None, "candidates": []}

        try:
            tracks = self._execute_search(q_str)
        except Exception:
            if attempt == 1:
                print("[MusicBrainz] Query failed, falling back to simple string")
                tracks = self._execute_search(f"{title} {uploader or ''}")
            else:
                raise

        return self._score_candidates(tracks, title)

    def search_raw(
        self, query_string: str, original_title: str, market: str = "DE"
    ) -> Dict[str, Any]:
        print(f"[MusicBrainz] Manual Search: {query_string}")
        tracks = self._execute_search(query_string)
        return self._score_candidates(tracks, original_title)
