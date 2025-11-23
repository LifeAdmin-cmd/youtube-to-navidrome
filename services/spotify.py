import base64
import os
import re
from typing import Any, Dict, Optional

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
        resp = requests.get(f"{self.BASE_API}/tracks/{track_id}", headers=headers)
        resp.raise_for_status()
        track = resp.json()

        # 2. Album Info
        album_info = track.get("album", {})
        if album_info.get("id"):
            try:
                a_resp = requests.get(
                    f"{self.BASE_API}/albums/{album_info['id']}", headers=headers
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
                    f"{self.BASE_API}/artists/{artists[0]['id']}", headers=headers
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
                "Title": track.get("name"),
                "Genre": genres[0] if genres else None,
                "Label": album_info.get("label"),
                "Release Date": album_info.get("release_date"),
                "Description": f"Spotify ID: {track_id}",
            }
        }

    def search_tracks(
        self, query: str, uploader: str = None, market: str = "DE"
    ) -> Dict[str, Any]:
        """
        Searches Spotify and returns the best match metadata AND a list of all candidates.
        """

        def word_match_ratio(text1: str, text2: str) -> float:
            words1 = set(re.findall(r"\w+", text1.lower()))
            words2 = set(re.findall(r"\w+", text2.lower()))
            intersection = words1.intersection(words2)
            total = len(words1) + len(words2)
            return (2.0 * len(intersection)) / total if total > 0 else 0.0

        headers = self._get_headers()
        q_str = f"{query} - {uploader}" if uploader else query
        params = {"q": q_str, "type": "track", "limit": 10, "market": market}

        print(f"[Spotify] Searching: {q_str}")
        resp = requests.get(self.SEARCH_API, headers=headers, params=params)
        tracks = resp.json().get("tracks", {}).get("items", [])

        # Retry logic if strict search fails
        if not tracks and uploader:
            print("[Spotify] Retrying without uploader...")
            params["q"] = query
            resp = requests.get(self.SEARCH_API, headers=headers, params=params)
            tracks = resp.json().get("tracks", {}).get("items", [])

        candidates = []
        best_track = None
        best_score = -1.0

        for track in tracks:
            t_name = track.get("name", "")
            artists = [a.get("name", "") for a in track.get("artists", [])]
            a_names = ", ".join(artists)

            # Calculate score
            full_str = f"{' '.join(artists)} {t_name}"
            score = word_match_ratio(query, full_str)

            # Get image
            images = track.get("album", {}).get("images", [])
            image_url = (
                images[-1].get("url") if images else None
            )  # Use smallest image for list

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
