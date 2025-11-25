import base64
import os
import re
from typing import Any, Dict, List, Optional

import requests


class SpotifyClient:
    """Handles Spotify Authentication and Search."""

    AUTH_URL = "https://accounts.spotify.com/api/token"
    BASE_API = "https://api.spotify.com/v1"
    SEARCH_API = "https://api.spotify.com/v1/search"

    def __init__(
        self, client_id: Optional[str] = None, client_secret: Optional[str] = None
    ):
        self.client_id = client_id or os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("SPOTIFY_CLIENT_SECRET")
        self._token = None

    def _get_token(self) -> str:
        if self._token:
            return self._token

        if not self.client_id or not self.client_secret:
            raise ValueError("Spotify credentials missing.")

        auth_str = f"{self.client_id}:{self.client_secret}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()

        headers = {
            "Authorization": f"Basic {auth_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials"}

        resp = requests.post(self.AUTH_URL, headers=headers, data=data, timeout=10)
        resp.raise_for_status()
        self._token = resp.json().get("access_token")
        return self._token

    def _get_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def get_track_metadata(self, track_id: str) -> Dict[str, Any]:
        headers = self._get_headers()

        # 1. Track Info
        resp = requests.get(
            f"{self.BASE_API}/tracks/{track_id}", headers=headers, timeout=10
        )
        resp.raise_for_status()
        track = resp.json()

        # 2. Album Info
        album_info = track.get("album", {})
        if album_info.get("id"):
            try:
                a_resp = requests.get(
                    f"{self.BASE_API}/albums/{album_info['id']}",
                    headers=headers,
                    timeout=10,
                )
                if a_resp.ok:
                    album_info = a_resp.json()
            except Exception:
                pass

        # 3. Artist/Genre Info
        genres = []
        artists = track.get("artists", [])
        if artists:
            try:
                ar_resp = requests.get(
                    f"{self.BASE_API}/artists/{artists[0]['id']}",
                    headers=headers,
                    timeout=10,
                )
                if ar_resp.ok:
                    genres = ar_resp.json().get("genres", [])
            except Exception:
                pass

        # Build Tags
        images = album_info.get("images", [])
        cover_art = images[0].get("url") if images else None
        artist_names = [a.get("name") for a in artists]

        return {
            "tags": {
                "Album Cover Art": cover_art,
                "Album": album_info.get("name"),
                "Artist": ", ".join(artist_names),
                "Artists": artist_names,  # <--- NEW: Pass the list of artists
                "Title": track.get("name"),
                "Genre": genres[0] if genres else None,
                "Label": album_info.get("label"),
                "Release Date": album_info.get("release_date"),
                "Description": f"Spotify ID: {track_id}",
            }
        }

    def _word_match_ratio(self, text1: str, text2: str) -> float:
        """Helper for scoring tracks."""
        words1 = set(re.findall(r"\w+", text1.lower()))
        words2 = set(re.findall(r"\w+", text2.lower()))
        intersection = words1.intersection(words2)
        total = len(words1) + len(words2)
        return (2.0 * len(intersection)) / total if total > 0 else 0.0

    def _score_candidates(
        self, tracks: List[Dict[str, Any]], target_title: str
    ) -> Dict[str, Any]:
        """Helper to score tracks and find the best match/candidates list."""

        candidates = []
        best_track = None
        best_score = -1.0

        for track in tracks:
            t_name = track.get("name", "")
            artists = [a.get("name", "") for a in track.get("artists", [])]
            a_names = ", ".join(artists)

            # Calculate score using the *target* title (YouTube title in most cases)
            full_str = f"{' '.join(artists)} {t_name}"
            score = self._word_match_ratio(target_title, full_str)

            # Get image
            images = track.get("album", {}).get("images", [])
            image_url = images[-1].get("url") if images else None

            candidate = {
                "id": track["id"],
                "title": t_name,
                "artist": a_names,
                "album": track.get("album", {}).get("name"),
                "image": image_url,
                "score": score,
            }
            candidates.append(candidate)

            if score > best_score:
                best_score = score
                best_track = track

        # Sort candidates by score
        candidates.sort(key=lambda x: x["score"], reverse=True)

        result = {"best_match": None, "candidates": candidates}

        if best_track:
            print(f"[Spotify] Match: '{best_track['name']}' (Score: {best_score:.2f})")
            result["best_match"] = self.get_track_metadata(best_track["id"])

        return result

    def _execute_search(self, query_string: str, market: str) -> List[Dict[str, Any]]:
        """Executes the Spotify API search and returns raw tracks."""
        headers = self._get_headers()
        params = {"q": query_string, "type": "track", "limit": 10, "market": market}

        resp = requests.get(self.SEARCH_API, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("tracks", {}).get("items", [])

    def search_tracks(
        self, title: str, uploader: str = None, market: str = "DE", attempt: int = 1
    ) -> Dict[str, Any]:
        """
        Searches Spotify using one of three pre-defined strategies based on 'attempt'.
        The scoring is always done against the original YouTube video title.
        """

        # --- Search Strategy Logic ---
        q_str = ""

        if attempt == 1:
            # Strategy 1: Strict match with uploader
            q_str = f"{title} - {uploader}" if uploader else title
            print(f"[Spotify] Attempt 1 Search (Title+Uploader): {q_str}")
        elif attempt == 2:
            # Strategy 2: Relaxed match (title only)
            q_str = title
            print(f"[Spotify] Attempt 2 Search (Title Only): {q_str}")
        elif attempt == 3:
            # Strategy 3: Looser match (title with common artifact removal)
            # Use regex to remove common tags/artifacts (e.g. [Official Video])
            loose_title = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", title).strip()
            q_str = loose_title or title
            print(f"[Spotify] Attempt 3 Search (Loose Title): {q_str}")
        else:
            return {"best_match": None, "candidates": []}

        try:
            tracks = self._execute_search(q_str, market)
        except requests.exceptions.RequestException as e:
            # Re-raise to be handled by the workflow's retry mechanism
            raise e

        return self._score_candidates(tracks, title)

    def search_raw(
        self, query_string: str, original_title: str, market: str = "DE"
    ) -> Dict[str, Any]:
        """Used for manual search; takes a user query string and scores against original_title."""

        print(f"[Spotify] Manual Search: {query_string}")
        try:
            tracks = self._execute_search(query_string, market)
        except requests.exceptions.RequestException as e:
            raise e

        return self._score_candidates(tracks, original_title)
