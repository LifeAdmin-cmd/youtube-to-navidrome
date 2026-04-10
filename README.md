# YouTube to Navidrome

A web-based tool to download music from YouTube, automatically clean up segments (SponsorBlock), tag metadata using the [ytmusicapi](https://github.com/sigma67/ytmusicapi) library, and organize files directly for your [Navidrome](https://www.navidrome.org/) library.

## 🚀 Features

* **YouTube Download:** Downloads audio from YouTube videos or entire playlists.
* **SponsorBlock Integration:** Automatically removes non-music segments like intros, outros, and self-promotions.
* **YTMusic Auto-Tagging:** Fetches accurate metadata (Artist, Title, Album, Cover Art) directly from YouTube Music.
* **Duplicate Detection:** Checks your existing Navidrome SQLite database to prevent downloading tracks that are already in your library.
* **Web Interface:** Simple UI to monitor progress, review tags, and retry failed downloads.

## 🐳 Docker Installation

The recommended way to run this application is via Docker. Since the tool uses the public YouTube Music API for tagging, **no API keys are required**.

**Note:** It is highly recommended to set `MAX_WORKERS` to `2`. Using a higher number may cause YouTube Music to rate-limit or block your IP address due to too many rapid requests.

### Run Command

Replace the paths with your own local directories:

```bash
docker run -d \
  --name youtube-to-navidrome \
  -p 5000:5000 \
  -v /path/to/your/music_library:/music/downloads \
  -v /path/to/navidrome/data/navidrome.db:/data/navidrome.db \
  -e output_directory="/music/downloads" \
  -e NAVIDROME_DB_PATH="/data/navidrome.db" \
  -e MAX_WORKERS=2 \
  lifeadmin/youtube-to-navidrome:v1.0.0