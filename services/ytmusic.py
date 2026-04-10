import re
from typing import Any, Dict, List

from ytmusicapi import YTMusic


class YTMusicClient:
    """Handles YouTube Music Search and Tagging."""

    def __init__(self):
        self.yt = YTMusic()

    def _get_high_res_cover(self, url: str) -> str:
        """Upgrades the image resolution to maximum."""
        if not url:
            return None
        if "=w" in url:
            return re.sub(r"=w\d+-h\d+", "=w1080-h1080", url)
        if "ytimg.com" in url:
            return url.replace("hqdefault.jpg", "maxresdefault.jpg").replace(
                "mqdefault.jpg", "maxresdefault.jpg"
            )
        return url

    def _word_match_ratio(self, text1: str, text2: str) -> float:
        """Helper for scoring tracks based on text overlap."""
        words1 = set(re.findall(r"\w+", text1.lower()))
        words2 = set(re.findall(r"\w+", text2.lower()))
        intersection = words1.intersection(words2)
        total = len(words1) + len(words2)
        return (2.0 * len(intersection)) / total if total > 0 else 0.0

    def _score_candidates(
        self, tracks: List[Dict[str, Any]], target_title: str
    ) -> Dict[str, Any]:
        """Helper to score tracks and format the candidate list for the UI."""
        candidates = []
        best_track = None
        best_score = -1.0

        for track in tracks:
            t_name = track.get("title", "Unknown")
            artists = [a.get("name", "") for a in track.get("artists", [])]
            a_names = ", ".join(artists)

            full_str = f"{' '.join(artists)} {t_name}"
            score = self._word_match_ratio(target_title, full_str)

            thumbnails = track.get("thumbnails", [])
            image_url = thumbnails[-1].get("url") if thumbnails else None
            high_res_img = self._get_high_res_cover(image_url)

            album_name = (
                track.get("album", {}).get("name") if track.get("album") else "Single"
            )
            year = track.get("year")
            video_id = track.get("videoId")

            # Store the exact tags needed by Mutagen inside the candidate
            tags = {
                "Album Cover Art": high_res_img,
                "Album": album_name,
                "Artist": a_names,
                "Artists": artists,
                "Title": t_name,
                "Genre": None,
                "Release Date": str(year) if year else None,
                "Description": f"YTMusic ID: {video_id}",
            }

            candidate = {
                "id": video_id,
                "title": t_name,
                "artist": a_names,
                "album": album_name,
                "image": high_res_img,
                "score": score,
                "tags": tags,
            }
            candidates.append(candidate)

            if score > best_score:
                best_score = score
                best_track = candidate

        # Sort candidates by score
        candidates.sort(key=lambda x: x["score"], reverse=True)

        result = {"best_match": best_track, "candidates": candidates}

        if best_track:
            print(f"[YTMusic] Match: '{best_track['title']}' (Score: {best_score:.2f})")

        return result

    def search_tracks(
        self, title: str, uploader: str = None, attempt: int = 1
    ) -> Dict[str, Any]:
        """Auto-search strategies."""
        q_str = ""
        if attempt == 1:
            q_str = f"{title} - {uploader}" if uploader else title
        elif attempt == 2:
            q_str = title
        elif attempt == 3:
            loose_title = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", title).strip()
            q_str = loose_title or title
        else:
            return {"best_match": None, "candidates": []}

        print(f"[YTMusic] Search Attempt {attempt}: {q_str}")
        try:
            tracks = self.yt.search(query=q_str, filter="songs", limit=10)
        except Exception as e:
            raise e

        return self._score_candidates(tracks, title)

    def search_raw(self, query_string: str, original_title: str) -> Dict[str, Any]:
        """Manual search executed via UI."""
        print(f"[YTMusic] Manual Search: {query_string}")
        try:
            tracks = self.yt.search(query=query_string, filter="songs", limit=10)
        except Exception as e:
            raise e
        return self._score_candidates(tracks, original_title)
