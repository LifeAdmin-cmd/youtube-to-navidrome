import base64
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from mutagen.flac import Picture
from mutagen.oggopus import OggOpus

from utils import Utils


class AudioProcessor:
    """Handles FFmpeg cutting and Mutagen tagging."""

    @staticmethod
    def cut_audio(
        input_path: Path, output_path: Path, segments: List[Tuple[float, float]]
    ):
        if not segments:
            shutil.copy(input_path, output_path)
            return

        filter_parts = []
        input_labels = []

        for i, (start, end) in enumerate(segments):
            label = f"a{i}"
            filter_parts.append(
                f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[{label}]"
            )
            input_labels.append(f"[{label}]")

        concat_cmd = (
            f"{''.join(input_labels)}concat=n={len(input_labels)}:v=0:a=1[outa]"
        )
        full_filter = ";".join(filter_parts) + ";" + concat_cmd

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-filter_complex",
            full_filter,
            "-map",
            "[outa]",
            "-vn",
            str(output_path),
        ]

        print(f"[FFmpeg] Processing {len(segments)} segments...")
        subprocess.run(
            cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )

    @staticmethod
    def tag_audio(file_path: Path, tags: Dict[str, Any]) -> Path:
        """Applies tags and renames the file."""
        try:
            audio = OggOpus(file_path)

            key_map = {
                "Title": "title",
                "Artist": "artist",
                "Album": "album",
                "Genre": "genre",
                "Label": "organization",
                "Release Date": "date",
                "Description": "description",
            }

            for k, v in key_map.items():
                if tags.get(k):
                    audio[v] = str(tags[k])

            # Cover Art
            cover_url = tags.get("Album Cover Art")
            if cover_url:
                try:
                    r = requests.get(
                        cover_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10
                    )
                    if r.status_code == 200:
                        pic = Picture()
                        pic.type = 3
                        pic.mime = (
                            "image/png" if cover_url.endswith("png") else "image/jpeg"
                        )
                        pic.desc = "Cover Art"
                        pic.data = r.content
                        audio["metadata_block_picture"] = [
                            base64.b64encode(pic.write()).decode("ascii")
                        ]
                except Exception as e:
                    print(f"[Tagging] Cover art error: {e}")

            audio.save()

            # Rename
            artist = Utils.sanitize_filename(tags.get("Artist", ""))
            title = Utils.sanitize_filename(tags.get("Title", ""))

            new_stem = (
                f"{artist} - {title}"
                if artist and title
                else Utils.sanitize_filename(tags.get("Title", file_path.stem))
            )
            new_path = Utils.unique_path(
                file_path.with_name(f"{new_stem}{file_path.suffix}")
            )

            file_path.rename(new_path)
            return new_path

        except Exception as e:
            print(f"[Tagging] Error: {e}")
            return file_path

    @staticmethod
    def check_duplicate(directory: Path, title: str, album: str) -> bool:
        if not directory.exists():
            return False

        norm_title = Utils.normalize_text(title)
        norm_album = Utils.normalize_text(album)

        for f in directory.glob("*.opus"):
            try:
                # Basic optimization
                if norm_title not in Utils.normalize_text(f.name):
                    pass

                audio = OggOpus(f)
                f_titles = [Utils.normalize_text(t) for t in audio.get("title", [])]
                f_albums = [Utils.normalize_text(a) for a in audio.get("album", [])]

                if norm_title in f_titles:
                    if not norm_album or norm_album in f_albums:
                        return True
            except Exception:
                continue
        return False
