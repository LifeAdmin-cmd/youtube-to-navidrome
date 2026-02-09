import base64
import os
import sqlite3
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
        cmd = ["ffmpeg", "-y", "-i", str(input_path)]

        # Target -2dB True Peak using loudnorm
        # I=-16 is a standard integrated loudness target for web/mobile
        norm_filter = "loudnorm=I=-16:TP=-2:LRA=11"

        if segments:
            filter_parts = []
            input_labels = []
            for i, (start, end) in enumerate(segments):
                label = f"a{i}"
                filter_parts.append(
                    f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[{label}]"
                )
                input_labels.append(f"[{label}]")

            concat_cmd = (
                f"{''.join(input_labels)}concat=n={len(input_labels)}:v=0:a=1[cuta]"
            )
            # Chain the cut audio into loudnorm
            full_filter = (
                f"{';'.join(filter_parts)};{concat_cmd};[cuta]{norm_filter}[outa]"
            )
        else:
            # Just normalize the original stream
            full_filter = f"[0:a]{norm_filter}[outa]"

        cmd.extend(
            [
                "-filter_complex",
                full_filter,
                "-map",
                "[outa]",
                "-vn",
                str(output_path),
            ]
        )

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

            # Set 'ARTISTS' tag if there is more than one artist
            if (
                tags.get("Artists")
                and isinstance(tags["Artists"], list)
                and len(tags["Artists"]) > 1
            ):
                audio["ARTISTS"] = tags["Artists"]

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
    def check_duplicate(title: str, artist: str, album: str = None) -> bool:
        """
        Checks if the song already exists in the Navidrome database.
        Uses environment variable NAVIDROME_DB_PATH for the database location.
        """
        db_path_str = os.getenv("NAVIDROME_DB_PATH")
        if not db_path_str:
            print("[DuplicateCheck] NAVIDROME_DB_PATH not set. Skipping DB check.")
            return False

        db_path = Path(db_path_str)
        if not db_path.exists():
            print(f"[DuplicateCheck] Database not found at {db_path}. Skipping check.")
            return False

        try:
            with sqlite3.connect(db_path) as conn:
                # Register the normalize function to be used in SQL queries
                conn.create_function("normalize", 1, Utils.normalize_text)
                cursor = conn.cursor()

                # Introspect media_file columns to determine schema structure
                cursor.execute("PRAGMA table_info(media_file)")
                columns = [info[1] for info in cursor.fetchall()]

                query_parts = ["SELECT 1 FROM media_file m"]
                joins = []
                wheres = ["normalize(m.title) = normalize(?)"]
                params = [title]

                # Handle Artist Check
                if "artist" in columns:
                    # Flat schema or cached column
                    wheres.append("normalize(m.artist) = normalize(?)")
                    params.append(artist)
                elif "artist_id" in columns:
                    # Normalized schema
                    joins.append("JOIN artist a ON m.artist_id = a.id")
                    wheres.append("normalize(a.name) = normalize(?)")
                    params.append(artist)

                # Handle Album Check
                if album:
                    if "album" in columns:
                        wheres.append("normalize(m.album) = normalize(?)")
                        params.append(album)
                    elif "album_id" in columns:
                        joins.append("LEFT JOIN album al ON m.album_id = al.id")
                        wheres.append("normalize(al.name) = normalize(?)")
                        params.append(album)

                # Assemble Query
                full_query = f"{' '.join(query_parts)} {' '.join(joins)} WHERE {' AND '.join(wheres)}"

                cursor.execute(full_query, params)
                result = cursor.fetchone()

                return result is not None

        except Exception as e:
            print(f"[DuplicateCheck] Database error: {e}")
            return False
