import base64
import difflib
import json
import os
import re
import shutil
import string
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
import yt_dlp as ytdlp
from dotenv import load_dotenv
from mutagen.flac import Picture
from mutagen.oggopus import OggOpus

# Load environment variables
load_dotenv()

# ==========================================
# PART 1: SponsorBlock & Cutting Logic
# ==========================================


def get_sponsorblock_segments(
    video_id: str, categories: List[str]
) -> List[Tuple[float, float]]:
    """
    Query SponsorBlock API directly for specific categories.
    Returns a list of (start, end) tuples to REMOVE.
    """
    base_url = "https://sponsor.ajay.app/api/skipSegments"
    params = {"videoID": video_id, "categories": json.dumps(categories)}
    query_string = urllib.parse.urlencode(params)
    api_url = f"{base_url}?{query_string}"

    try:
        with urllib.request.urlopen(api_url) as response:
            data = json.loads(response.read().decode())

        segments = []
        for segment in data:
            start, end = segment["segment"]
            category = segment["category"]
            segments.append((start, end))
            print(
                f"[SponsorBlock] Found segment to remove: {start}s - {end}s ({category})"
            )

        segments.sort(key=lambda x: x[0])
        return segments

    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[SponsorBlock] No segments found for this video.")
            return []
        print(f"[SponsorBlock] API Error: {e}")
        return []
    except Exception as e:
        print(f"[SponsorBlock] Connection Error: {e}")
        return []


def invert_segments(
    total_duration: float, remove_segments: List[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """
    Takes a list of segments to REMOVE and calculates the segments to KEEP.
    """
    keep_segments = []
    current_pos = 0.0

    for start, end in remove_segments:
        if start > current_pos:
            keep_segments.append((current_pos, start))
        current_pos = max(current_pos, end)

    if current_pos < total_duration:
        keep_segments.append((current_pos, total_duration))

    return keep_segments


def cut_audio_manually(
    input_path: Path, output_path: Path, keep_segments: List[Tuple[float, float]]
):
    """
    Uses FFmpeg 'atrim' and 'concat' filters to stitch together the good parts.
    """
    if not keep_segments:
        print("[Processing] No cuts needed. Copying file.")
        shutil.copy(input_path, output_path)
        return

    filter_parts = []
    input_labels = []

    for i, (start, end) in enumerate(keep_segments):
        label = f"a{i}"
        filter_parts.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[{label}]"
        )
        input_labels.append(f"[{label}]")

    concat_cmd = f"{''.join(input_labels)}concat=n={len(input_labels)}:v=0:a=1[outa]"
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

    print(f"[Processing] Running Manual FFmpeg Cut ({len(keep_segments)} parts)...")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def download_and_cut(
    url: str,
    out_dir: Path,
    audio_format: str = "opus",
    video_info: Optional[Dict] = None,
) -> Path:
    out_path = out_dir
    out_path.mkdir(parents=True, exist_ok=True)

    # Use provided info or fetch if missing
    if not video_info:
        with ytdlp.YoutubeDL({"quiet": True}) as ydl:
            video_info = ydl.extract_info(url, download=False)

    video_id = video_info["id"]
    duration = video_info["duration"]
    title_safe = _sanitize_filename(video_info["title"])

    print("--- 1. Downloading Original Audio ---")
    ydl_opts = {
        "outtmpl": str(out_path / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "format": "bestaudio/best",
        "overwrites": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "192",
            }
        ],
    }

    with ytdlp.YoutubeDL(ydl_opts) as ydl:
        # We download using the ID to ensure we catch the file we expect
        ydl.download([url])
        # Calculate expected filename based on sanitize template
        downloaded_file = out_path / f"{title_safe}.{audio_format}"

    # Check if file exists, sometimes ytdlp naming varies slightly
    if not downloaded_file.exists():
        # Fallback: find the most recently modified file in dir
        downloaded_file = max(out_path.glob(f"*.{audio_format}"), key=os.path.getctime)

    print(f"--- 2. Fetching Cuts for ID: {video_id} ---")
    categories = ["music_offtopic", "intro", "outro", "selfpromo", "interaction"]
    bad_segments = get_sponsorblock_segments(video_id, categories)

    print("--- 3. Calculating Cuts ---")
    good_segments = invert_segments(duration, bad_segments)

    if (
        len(good_segments) == 1
        and good_segments[0][0] == 0
        and good_segments[0][1] == duration
    ):
        print("--- No cuts required. Done. ---")
        return downloaded_file

    print(f"--- 4. Applying Cuts (Segments: {len(good_segments)}) ---")
    base_name = downloaded_file.stem
    final_file = out_path / f"{base_name}_cut.{audio_format}"

    try:
        cut_audio_manually(downloaded_file, final_file, good_segments)
        downloaded_file.unlink()  # Remove original
        final_file.rename(downloaded_file)  # Rename cut to original name
        print(f"--- Success! Saved to: {downloaded_file} ---")
        return downloaded_file

    except subprocess.CalledProcessError as e:
        print("FFmpeg failed.")
        raise e


# ==========================================
# PART 2: Spotify Metadata Logic
# ==========================================


def _get_spotify_access_token(
    client_id: Optional[str] = None, client_secret: Optional[str] = None
) -> str:
    client_id = client_id or os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = client_secret or os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError(
            "Spotify client_id and client_secret must be provided or set in environment variables."
        )

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    resp = requests.post(
        "https://accounts.spotify.com/api/token", headers=headers, data=data
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Failed to obtain Spotify access token.")
    return token


def _extract_spotify_track_id(track_id_or_url: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9]{22}", track_id_or_url):
        return track_id_or_url
    m = re.search(r"spotify:track:([A-Za-z0-9]{22})", track_id_or_url)
    if m:
        return m.group(1)
    m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]{22})", track_id_or_url)
    if m:
        return m.group(1)
    m = re.search(r"track/([A-Za-z0-9]{22})", track_id_or_url)
    if m:
        return m.group(1)
    raise ValueError("Could not parse track id from input.")


def get_spotify_audio_tags(
    track_id_or_url: str,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    include_audio_features: bool = True,
) -> Dict[str, Any]:
    track_id = _extract_spotify_track_id(track_id_or_url)
    token = _get_spotify_access_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api.spotify.com/v1"

    out: Dict[str, Any] = {}

    # 1. Track Metadata
    t_resp = requests.get(f"{base}/tracks/{track_id}", headers=headers)
    t_resp.raise_for_status()
    track = t_resp.json()
    out["track"] = track

    # 2. Album Info
    album_info = track.get("album") or {}
    album_id = album_info.get("id")
    if album_id:
        try:
            a_resp = requests.get(f"{base}/albums/{album_id}", headers=headers)
            if a_resp.status_code == 200:
                album_info = a_resp.json()
        except Exception as e:
            print(f"[Spotify] Album fetch warning: {e}")

    # 3. Genre
    genres: List[str] = []
    artists = track.get("artists") or []
    if artists:
        primary_id = artists[0].get("id")
        if primary_id:
            try:
                ar_resp = requests.get(f"{base}/artists/{primary_id}", headers=headers)
                if ar_resp.status_code == 200:
                    genres = ar_resp.json().get("genres", []) or []
            except Exception:
                pass

    # 4. Audio Features
    audio_features = None
    if include_audio_features:
        try:
            af_resp = requests.get(f"{base}/audio-features/{track_id}", headers=headers)
            if af_resp.status_code == 200:
                audio_features = af_resp.json()
        except Exception:
            pass

    # Build simplified tags
    images = (album_info.get("images") if isinstance(album_info, dict) else []) or []
    cover_art = images[0].get("url") if images else None

    artist_names = [a.get("name") for a in artists if a.get("name")]

    tags = {
        "Album Cover Art": cover_art,
        "Album": album_info.get("name"),
        "Artist": ", ".join(artist_names) if artist_names else None,
        "Title": track.get("name"),
        "Genre": genres[0] if genres else None,
        "Label": album_info.get("label") if isinstance(album_info, dict) else None,
        "Release Date": album_info.get("release_date"),
        "Description": f"Spotify ID: {track_id}",
    }

    out["tags"] = tags
    return out


def parse_artist_title_from_filename(filename: str) -> Tuple[str, str]:
    name = Path(filename).stem
    # Remove content in brackets/parentheses
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", name).strip()

    # Common separators
    separators = [" - ", " — ", " – ", ":"]
    for sep in separators:
        if sep in name:
            parts = [p.strip() for p in name.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[0], " - ".join(parts[1:]) if len(parts) > 2 else parts[1]

    return "", name


def _normalize_text(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = s.translate(str.maketrans("", "", string.punctuation))
    return " ".join(s.split())


def _best_match_in_album(
    album_id: str, target_title: str, token: str, market: str = "US"
) -> Optional[str]:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/albums/{album_id}/tracks"
    params = {"limit": 50, "market": market}

    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        return None

    items = resp.json().get("items", [])
    norm_target = _normalize_text(target_title)
    best_id = None
    best_score = 0.0

    for it in items:
        score = difflib.SequenceMatcher(
            None, norm_target, _normalize_text(it.get("name", ""))
        ).ratio()
        if score > best_score:
            best_score = score
            best_id = it.get("id")

    return best_id


def find_spotify_track_by_search(
    query_str: str,
    artist: str = None,
    uploader: str = None,
    client_id=None,
    client_secret=None,
    market="DE",
) -> Optional[Dict[str, Any]]:
    """
    Refined search function.
    Uses Title + Uploader (Channel) if available to improve accuracy.
    Selects the best match from top 10 results based on string similarity.
    """

    # --- 1. CLEANING LOGIC ---
    # Helper function to safely strip brackets iteratively
    def clean_brackets(text: str) -> str:
        clean = text
        # We loop until the string stops changing.
        # This handles nested brackets like "(Title (Remix))" correctly.
        while True:
            prev = clean
            # Remove (...) - strictly matching pairs
            clean = re.sub(r"\s*\([^)]*\)", " ", clean)
            # Remove [...] - strictly matching pairs
            clean = re.sub(r"\s*\[[^]]*\]", " ", clean)
            # Remove {...} - strictly matching pairs
            clean = re.sub(r"\s*\{[^}]*\}", " ", clean)

            # Collapse extra spaces
            clean = " ".join(clean.split())

            if clean == prev:
                break
        return clean

    # Prepare base query
    q = ""

    if artist:
        q = f'track:"{query_str}" artist:"{artist}"'
    else:
        # Try to parse artist/title if it looks like a filename
        p_artist, p_title = parse_artist_title_from_filename(query_str)
        if p_artist and p_title:
            q = f'track:"{p_title}" artist:"{p_artist}"'
        else:
            # --- APPLY NEW CLEANING HERE ---
            clean_query = clean_brackets(query_str)

            # Append YouTube Channel Name to query for better context
            if uploader:
                # Clean uploader name (remove generic terms like VEVO or Topic)
                clean_uploader = re.sub(
                    r"\s*(VEVO|Topic|Official|Music)\s*",
                    "",
                    uploader,
                    flags=re.IGNORECASE,
                ).strip()
                if clean_uploader:
                    clean_query = f"{clean_query} {clean_uploader}"

            q = clean_query

    token = _get_spotify_access_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}

    q = f"{query_str} - {uploader}" if uploader else query_str
    print(f"[Spotify Search] Querying Spotify with: {q}")

    # Increased limit to 10 to widen the net
    params = {"q": q, "type": "track", "limit": 10, "market": market}

    resp = requests.get(
        "https://api.spotify.com/v1/search", headers=headers, params=params
    )
    if resp.status_code != 200:
        return None

    tracks = resp.json().get("tracks", {}).get("items", [])

    # Fallback: if strict search failed, try looser search on just title
    if not tracks:
        search_term = (
            p_title if (not artist and "p_title" in locals() and p_title) else query_str
        )
        # Apply the same iterative cleaning to the fallback search
        search_term = clean_brackets(search_term)

        params["q"] = search_term
        resp = requests.get(
            "https://api.spotify.com/v1/search", headers=headers, params=params
        )
        if resp.status_code == 200:
            tracks = resp.json().get("tracks", {}).get("items", [])

            for t in tracks:
                print(
                    f"[Spotify Fallback] Candidate: {t.get('name')} by {[a.get('name') for a in t.get('artists', [])]}"
                )

    if not tracks:
        return None

    # --- SIMILARITY CHECK LOOP ---
    print(f"[Spotify Search] Found {len(tracks)} candidates. Comparing similarity...")

    best_track = None
    best_score = -1.0

    # Compare against the cleaned query (without the artist/uploader extras) for purity
    target_text = _normalize_text(clean_brackets(query_str))

    for track in tracks:
        track_name = track.get("name", "")
        artists = [a.get("name", "") for a in track.get("artists", [])]
        artist_str = " ".join(artists)

        # Compare: "Artist Title" vs Target
        spotify_text_full = _normalize_text(f"{artist_str} {track_name}")
        # Compare: "Title" vs Target
        spotify_text_title = _normalize_text(track_name)

        score_full = difflib.SequenceMatcher(
            None, target_text, spotify_text_full
        ).ratio()
        score_title = difflib.SequenceMatcher(
            None, target_text, spotify_text_title
        ).ratio()

        current_score = max(score_full, score_title)

        if current_score > best_score:
            best_score = current_score
            best_track = track

    top = best_track
    print(f"[Spotify Search] Selected: '{top['name']}' (Score: {best_score:.2f})")
    # ----------------------------------

    if top.get("type") == "album":
        chosen_id = _best_match_in_album(top.get("id"), query_str, token, market)
        return {"id": chosen_id} if chosen_id else None

    return top


def get_spotify_tags_from_search(
    query: str, uploader: str = None
) -> Optional[Dict[str, Any]]:
    print(f"--- Searching Spotify for: {query} (Uploader: {uploader}) ---")
    track_or_id = find_spotify_track_by_search(query, uploader=uploader)

    if not track_or_id:
        print(f"[Spotify Search] No match found for: {query}")
        return None

    track_id = track_or_id.get("id")
    if not track_id:
        return None

    return get_spotify_audio_tags(track_id)


# ==========================================
# PART 3: Tagging, Renaming & Checking Logic
# ==========================================


def _sanitize_filename(s: str, max_len: int = 200) -> str:
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", s)  # Remove illegal chars
    s = re.sub(r"\s+", " ", s)
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.parent / f"{path.stem} ({i}){path.suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def check_if_song_exists(directory: Path, target_title: str, target_album: str) -> bool:
    """
    Scans .opus files in the directory.
    Returns True if a file with matching Title and Album metadata is found.
    """
    if not directory.exists():
        return False

    print(
        f"[Check] Scanning directory for existing file: '{target_title}' on '{target_album}'"
    )

    norm_title = _normalize_text(target_title)
    norm_album = _normalize_text(target_album)

    for file_path in directory.glob("*.opus"):
        try:
            # OggOpus file reading
            audio = OggOpus(file_path)

            # Mutagen returns lists for tags (e.g. ['My Song'])
            existing_titles = audio.get("title", [])
            existing_albums = audio.get("album", [])

            if not existing_titles:
                continue

            # Check Title Match
            title_match = False
            for t in existing_titles:
                if _normalize_text(t) == norm_title:
                    title_match = True
                    break

            if not title_match:
                continue

            # Check Album Match (if target album is provided)
            if norm_album:
                album_match = False
                if not existing_albums:
                    pass

                for a in existing_albums:
                    if _normalize_text(a) == norm_album:
                        album_match = True
                        break

                if title_match and album_match:
                    print(f"[Check] Found duplicate: {file_path.name}")
                    return True
            elif title_match:
                # If we are only looking for title
                print(f"[Check] Found duplicate (Title match): {file_path.name}")
                return True

        except Exception:
            continue

    return False


def set_audio_tags(
    input_path: Path, tags: Dict[str, Any], overwrite: bool = True
) -> Path:
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"{input_path} does not exist")

    if not overwrite:
        target_path = input_path.with_name(
            input_path.stem + "_tagged" + input_path.suffix
        )
        shutil.copy2(input_path, target_path)
    else:
        target_path = input_path

    try:
        audio = OggOpus(target_path)

        # Text Tags
        key_map = {
            "Title": "title",
            "Artist": "artist",
            "Album": "album",
            "Genre": "genre",
            "Label": "organization",
            "Release Date": "date",
            "Description": "description",
        }

        for friendly, vorbis in key_map.items():
            val = tags.get(friendly)
            if val:
                audio[vorbis] = str(val)

        # Cover Art
        cover_url = tags.get("Album Cover Art")
        if cover_url:
            try:
                req = urllib.request.Request(
                    cover_url, headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req) as response:
                    image_data = response.read()

                pic = Picture()
                pic.type = 3  # Front Cover
                pic.mime = "image/png" if cover_url.endswith("png") else "image/jpeg"
                pic.desc = "Cover Art"
                pic.data = image_data

                encoded_data = base64.b64encode(pic.write()).decode("ascii")
                audio["metadata_block_picture"] = [encoded_data]
            except Exception as e:
                print(f"[Tagging] Warning: Could not embed cover art: {e}")

        audio.save()

        # Rename file
        artists = _sanitize_filename(tags.get("Artist", ""))
        title = _sanitize_filename(tags.get("Title", ""))

        if artists and title:
            new_stem = f"{artists} - {title}"
        else:
            new_stem = _sanitize_filename(tags.get("Title") or input_path.stem)

        new_path = _unique_path(target_path.with_name(new_stem + input_path.suffix))

        target_path.rename(new_path)
        return new_path

    except Exception as e:
        print(f"[Tagging] Critical Error: {e}")
        return input_path


# ==========================================
# PART 4: Coordinator Function (Generator Version)
# ==========================================


def _process_single_video(
    video_url: str, pre_fetched_info: Optional[Dict] = None
) -> Iterator[str]:
    """
    Coordinates process for ONE video.
    YIELDS JSON strings for the web UI.
    """

    # 1. Setup Output Directory from ENV
    env_dir = os.getenv("output_directory")
    if not env_dir:
        output_dir = Path(".")
    else:
        output_dir = Path(env_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # 2. Fetch Video Info
    try:
        if pre_fetched_info:
            info = pre_fetched_info
        else:
            yield json.dumps(
                {"status": "processing", "message": "Fetching metadata..."}
            )
            with ytdlp.YoutubeDL({"quiet": True}) as ydl:
                info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        yield json.dumps(
            {"status": "error", "message": f"Could not fetch info for {video_url}: {e}"}
        )
        return

    video_title = info.get("title")
    video_uploader = info.get("uploader")
    yield json.dumps(
        {"status": "processing", "message": f"Found: {video_title} ({video_uploader})"}
    )

    # 3. Search Metadata & Check Duplicates
    spotify_data = None
    try:
        spotify_data = get_spotify_tags_from_search(
            video_title, uploader=video_uploader
        )
    except Exception as e:
        print(f"Metadata pre-search failed: {e}")

    # Duplicate Check
    if spotify_data and "tags" in spotify_data:
        t_tags = spotify_data["tags"]
        s_title = t_tags.get("Title")
        s_album = t_tags.get("Album")

        if s_title and check_if_song_exists(output_dir, s_title, s_album or ""):
            yield json.dumps(
                {
                    "status": "skipped",
                    "message": f"Skipped: '{s_title}' already exists.",
                }
            )
            return
    else:
        yield json.dumps(
            {
                "status": "processing",
                "message": "No Spotify match found. downloading raw...",
            }
        )

    # 4. Download and Cut
    try:
        yield json.dumps(
            {"status": "processing", "message": f"Downloading {video_title}..."}
        )
        raw_file_path = download_and_cut(
            video_url, out_dir=output_dir, audio_format="opus", video_info=info
        )
    except Exception as e:
        yield json.dumps(
            {"status": "error", "message": f"Download failed for {video_title}: {e}"}
        )
        return

    # 5. Tag and Rename
    final_name = raw_file_path.name
    if spotify_data and "tags" in spotify_data:
        yield json.dumps({"status": "processing", "message": "Tagging file..."})
        final_path = set_audio_tags(raw_file_path, spotify_data["tags"], overwrite=True)
        final_name = final_path.name
        yield json.dumps(
            {
                "status": "success",
                "message": f"Successfully processed: {final_name}",
            }
        )
    else:
        yield json.dumps(
            {
                "status": "success",
                "message": f"Downloaded (No Tags): {final_name}",
            }
        )


def process_input_url(url: str) -> Iterator[str]:
    """
    Main Generator Entrypoint.
    Yields JSON updates for the UI.
    """

    # Send initial finding message
    yield json.dumps(
        {"status": "processing", "message": "Analyzing URL...", "progress": 0}
    )

    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "ignoreerrors": True,
    }

    entries = []
    is_playlist = False

    try:
        with ytdlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if "entries" in info:
            is_playlist = True
            entries = list(info["entries"])
            yield json.dumps(
                {
                    "status": "processing",
                    "message": f"Playlist detected: {len(entries)} items.",
                    "progress": 0,
                }
            )
        else:
            entries = [info]

    except Exception as e:
        yield json.dumps(
            {"status": "fatal_error", "message": f"Failed to parse URL: {e}"}
        )
        return

    total_count = len(entries)

    # Loop through all videos
    for index, entry in enumerate(entries):
        if not entry:
            continue

        # Calculate current progress percentage
        current_percent = int((index / total_count) * 100)

        # Get URL
        video_url = entry.get("url")
        if not video_url and entry.get("id"):
            video_url = f"https://www.youtube.com/watch?v={entry.get('id')}"

        if not video_url:
            continue

        # Determine if we have full info (single video) or need to fetch (playlist)
        # For single video, 'entry' is the full info. For playlist, it's flat info.
        pre_info = entry if not is_playlist else None

        # Run the single video processor and yield its messages
        # We catch the yields from _process_single_video and re-yield them with progress attached
        for update_json in _process_single_video(video_url, pre_fetched_info=pre_info):
            data = json.loads(update_json)
            # Inject global progress
            data["progress"] = current_percent
            yield json.dumps(data)

    # Final success message
    yield json.dumps(
        {"status": "finished", "message": "All operations completed!", "progress": 100}
    )


# ==========================================
# CLI Entry Point (Still works for testing)
# ==========================================
if __name__ == "__main__":
    # If running manually, we just consume the generator and print messages
    target_url = input("Enter YouTube URL (Video or Playlist): ").strip()
    if target_url:
        for update in process_input_url(target_url):
            data = json.loads(update)
            print(f"[{data.get('status').upper()}] {data.get('message')}")
