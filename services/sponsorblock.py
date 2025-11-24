import json
from typing import List, Tuple

import requests


class SponsorBlockClient:
    """Handles interaction with the SponsorBlock API."""

    BASE_URL = "https://sponsor.ajay.app/api/skipSegments"

    def get_segments(
        self, video_id: str, categories: List[str]
    ) -> List[Tuple[float, float]]:
        params = {"videoID": video_id, "categories": json.dumps(categories)}

        # Removed try/except block to allow errors to propagate to the workflow logger
        resp = requests.get(self.BASE_URL, params=params, timeout=10)

        if resp.status_code == 404:
            print("[SponsorBlock] No segments found.")
            return []

        resp.raise_for_status()

        data = resp.json()
        segments = []
        for segment in data:
            start, end = segment["segment"]
            print(
                f"[SponsorBlock] Found segment: {start}s - {end}s ({segment['category']})"
            )
            segments.append((float(start), float(end)))

        segments.sort(key=lambda x: x[0])
        return segments

    @staticmethod
    def invert_segments(
        total_duration: float, remove_segments: List[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        """Calculates parts to KEEP based on parts to REMOVE."""
        keep_segments = []
        current_pos = 0.0

        for start, end in remove_segments:
            if start > current_pos:
                keep_segments.append((current_pos, start))
            current_pos = max(current_pos, end)

        if current_pos < total_duration:
            keep_segments.append((current_pos, total_duration))

        return keep_segments
