import re
import time
from typing import Any, Dict, List

import requests


class MusicBrainzClient:
    """Handles MusicBrainz Search and Metadata Retrieval."""

    BASE_API = "https://musicbrainz.org/ws/2"
    COVER_ART_API = "https://coverartarchive.org"

    # MusicBrainz requires a unique User-Agent
    HEADERS = {
        "User-Agent": "YoutubeToNavidrome/1.0 ( generic_user@example.com )",
        "Accept": "application/json",
    }

    def __init__(self):
        # MusicBrainz doesn't require auth tokens for read-only access
        pass

    def _get(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Helper to make rate-limited requests to MusicBrainz."""
        url = f"{self.BASE_API}/{endpoint}"
        if params:
            # Add format=json to all requests
            params["fmt"] = "json"

        # Simple rate limiting (1 request per second is the polite standard)
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

        # 1. Fetch Recording Info (including artists, releases, and genres)
        data = self._get(f"recording/{mbid}", params={"inc": "artists+releases+genres"})

        title = data.get("title")

        # 2. Parse Artists
        artist_credits = data.get("artist-credit", [])
        artist_names = [
            ac.get("name")
            for ac in artist_credits
            if isinstance(ac, dict) and "name" in ac
        ]
        # Sometimes structure is deeper, handle simple name extraction
        if not artist_names and artist_credits:
            # Fallback for simple string lists or different structures
            artist_names = [
                x["artist"]["name"] for x in artist_credits if "artist" in x
            ]

        # 3. Choose the Best Release (Album)
        # Recordings can be on multiple releases (Singles, Albums, Compilations).
        # We prefer 'Official' albums.
        releases = data.get("releases", [])
        best_release = None

        # Sort/Filter logic: Prioritize Official Albums, then Official Singles, then others.
        for rel in releases:
            status = rel.get("status", "").lower()
            # Try to find date
            if status == "official":
                best_release = rel
                break

        if not best_release and releases:
            best_release = releases[0]

        album_name = best_release.get("title") if best_release else "Unknown Album"
        release_id = best_release.get("id") if best_release else None
        date = best_release.get("date") if best_release else None

        # 4. Fetch Cover Art (if we have a release ID)
        cover_art = None
        if release_id:
            try:
                # Cover Art Archive Lookup
                # Don't sleep for CA, it's a different server, but be safe.
                ca_url = f"{self.COVER_ART_API}/release/{release_id}"
                ca_resp = requests.get(ca_url, timeout=5)
                if ca_resp.ok:
                    images = ca_resp.json().get("images", [])
                    # Find 'Front'
                    for img in images:
                        if "Front" in img.get("types", []) or img.get("front"):
                            cover_art = img.get("image")
                            break
                    if not cover_art and images:
                        cover_art = images[0].get("image")
            except Exception:
                print(f"[MusicBrainz] No cover art found for release {release_id}")

        # 5. Genres (from recording or artist)
        genres = [t.get("name") for t in data.get("tags", [])]

        return {
            "tags": {
                "Album Cover Art": cover_art,
                "Album": album_name,
                "Artist": ", ".join(artist_names),
                "Artists": artist_names,
                "Title": title,
                "Genre": genres[0] if genres else None,
                "Label": None,  # Label info requires fetching release details separately usually
                "Release Date": date,
                "Description": f"MusicBrainz ID: {mbid}",
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
        self, recordings: List[Dict[str, Any]], target_title: str
    ) -> Dict[str, Any]:
        """Helper to score tracks and find the best match/candidates list."""

        candidates = []
        best_track = None
        best_score = -1.0

        for rec in recordings:
            t_name = rec.get("title", "")

            # Extract artist string
            artist_credits = rec.get("artist-credit", [])
            a_names_list = [
                x["artist"]["name"] for x in artist_credits if "artist" in x
            ]
            a_names = ", ".join(a_names_list)

            # Album name
            releases = rec.get("releases", [])
            album_name = releases[0].get("title") if releases else "Unknown"

            # Calculate score
            full_str = f"{a_names} {t_name}"
            score = self._word_match_ratio(target_title, full_str)

            candidate = {
                "id": rec["id"],
                "title": t_name,
                "artist": a_names,
                "album": album_name,
                "image": None,  # Search results don't give images easily, frontend handles placeholder
                "score": score,
            }
            candidates.append(candidate)

            if score > best_score:
                best_score = score
                best_track = candidate  # Store the simplified candidate or raw rec?
                # We need the ID for the next step, so candidate is fine,
                # but 'best_match' expects metadata. We fetch that below.

        # Sort candidates by score
        candidates.sort(key=lambda x: x["score"], reverse=True)

        result = {"best_match": None, "candidates": candidates}

        if best_track and best_score > 0.4:  # Minimal threshold
            print(
                f"[MusicBrainz] Match: '{best_track['title']}' (Score: {best_score:.2f})"
            )
            # Fetch full metadata for the best match
            try:
                meta = self.get_track_metadata(best_track["id"])
                meta["score"] = best_score
                result["best_match"] = meta
            except Exception as e:
                print(f"[MusicBrainz] Failed to fetch metadata for best match: {e}")

        return result

    def _execute_search(self, query: str) -> List[Dict[str, Any]]:
        """Executes the MusicBrainz API search."""
        # Lucene search syntax
        data = self._get("recording", params={"query": query, "limit": 10})
        return data.get("recordings", [])

    def search_tracks(
        self, title: str, uploader: str = None, market: str = "DE", attempt: int = 1
    ) -> Dict[str, Any]:
        """
        Searches MusicBrainz.
        """
        # Clean title of common youtube garbage for better search
        clean_title = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", title).strip()

        q_str = ""

        if attempt == 1:
            # Strategy 1: Title AND Artist (if uploader looks like an artist)
            # We treat uploader as artist.
            if uploader and "topic" not in uploader.lower():
                q_str = f'recording:"{clean_title}" AND artist:"{uploader}"'
            else:
                q_str = f'recording:"{clean_title}"'
            print(f"[MusicBrainz] Attempt 1 Search: {q_str}")

        elif attempt == 2:
            # Strategy 2: Title only (strict)
            q_str = f'recording:"{clean_title}"'
            print(f"[MusicBrainz] Attempt 2 Search: {q_str}")

        elif attempt == 3:
            # Strategy 3: Loose search (just the string)
            q_str = clean_title
            print(f"[MusicBrainz] Attempt 3 Search: {q_str}")

        else:
            return {"best_match": None, "candidates": []}

        try:
            tracks = self._execute_search(q_str)
        except Exception:
            # If complex query fails, fallback to simple string
            if attempt == 1:
                print("[MusicBrainz] Query failed, falling back to simple string")
                tracks = self._execute_search(f"{title} {uploader or ''}")
            else:
                raise

        return self._score_candidates(tracks, title)

    def search_raw(
        self, query_string: str, original_title: str, market: str = "DE"
    ) -> Dict[str, Any]:
        """Manual search."""
        print(f"[MusicBrainz] Manual Search: {query_string}")
        tracks = self._execute_search(query_string)
        return self._score_candidates(tracks, original_title)
