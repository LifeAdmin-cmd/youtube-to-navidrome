import re
import time
from typing import Any, Dict, List

import requests


class MusicBrainzClient:
    """Handles MusicBrainz Search and Metadata Retrieval."""

    BASE_API = "https://musicbrainz.org/ws/2"
    COVER_ART_API = "https://coverartarchive.org"

    HEADERS = {
        "User-Agent": "YoutubeToNavidrome/1.0 ( generic_user@example.com )",
        "Accept": "application/json",
    }

    def __init__(self):
        pass

    def _get(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Helper to make rate-limited requests to MusicBrainz."""
        url = f"{self.BASE_API}/{endpoint}"
        if params:
            params["fmt"] = "json"

        time.sleep(1.1)

        try:
            resp = requests.get(url, headers=self.HEADERS, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"[MusicBrainz] Request Error: {e}")
            raise e

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

    def _score_candidates(
        self, recordings: List[Dict[str, Any]], target_title: str
    ) -> Dict[str, Any]:
        """Helper to score tracks and find the best match/candidates list."""

        candidates = []
        best_track = None
        best_score = -1.0

        for rec in recordings:
            t_name = rec.get("title", "")

            artist_credits = rec.get("artist-credit", [])
            a_names_list = [
                x["artist"]["name"] for x in artist_credits if "artist" in x
            ]
            a_names = ", ".join(a_names_list)

            # --- CHANGED: Extract Release ID and construct Image URL ---
            releases = rec.get("releases", [])
            album_name = "Unknown"
            image_url = None

            if releases:
                # Try to find an official release, otherwise take the first one
                best_rel = next(
                    (r for r in releases if r.get("status") == "Official"), releases[0]
                )
                album_name = best_rel.get("title")
                rid = best_rel.get("id")
                # Construct optimistic URL (Browser will handle 404 if not found)
                if rid:
                    image_url = f"https://coverartarchive.org/release/{rid}/front-250"
            # -----------------------------------------------------------

            full_str = f"{a_names} {t_name}"
            score = self._word_match_ratio(target_title, full_str)

            candidate = {
                "id": rec["id"],
                "title": t_name,
                "artist": a_names,
                "album": album_name,
                "image": image_url,  # Now contains a URL or None
                "score": score,
            }
            candidates.append(candidate)

            if score > best_score:
                best_score = score
                best_track = candidate

        candidates.sort(key=lambda x: x["score"], reverse=True)

        result = {"best_match": None, "candidates": candidates}

        if best_track and best_score > 0.4:
            print(
                f"[MusicBrainz] Match: '{best_track['title']}' (Score: {best_score:.2f})"
            )
            try:
                meta = self.get_track_metadata(best_track["id"])
                meta["score"] = best_score
                result["best_match"] = meta
            except Exception as e:
                print(f"[MusicBrainz] Failed to fetch metadata for best match: {e}")

        return result

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
