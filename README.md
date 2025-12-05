# YouTube to Navidrome

A web-based tool to download music from YouTube, automatically clean up segments (SponsorBlock), tag metadata using the **Spotify API**, and organize files directly for your [Navidrome](https://www.navidrome.org/) library.

## 🚀 Features

* **YouTube Download:** Downloads audio from YouTube videos or entire playlists.
* **SponsorBlock Integration:** Automatically removes non-music segments like intros, outros, and self-promotions.
* **Spotify Auto-Tagging:** Fetches accurate metadata (Artist, Title, Album, Cover Art) directly from Spotify.
* **Duplicate Detection:** Checks your existing Navidrome SQLite database to prevent downloading tracks that are already in your library.
* **Web Interface:** Simple UI to monitor progress, review tags, and retry failed downloads.

## 🛠️ Configuration: Getting Spotify Credentials

Since this project uses Spotify for metadata tagging, you need to generate your own API credentials.

1.  Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/) and log in.
2.  Click **"Create App"**.
3.  Enter an **App Name** (e.g., "YouTube Tagger") and **Description**, then click **Save**.
4.  In your new app's dashboard, click **"Settings"**.
5.  Here you will find your **Client ID** and **Client Secret** (click "View client secret").
6.  Copy these keys; you will need them for the Docker command or your `.env` file.

## 🐳 Docker Installation

The recommended way to run this application is via Docker.

**Note:** It is highly recommended to set `MAX_WORKERS` to `2`. Using a higher number may cause the Spotify API to rate-limit or block your IP address due to too many rapid requests.

### Run Command

Replace the paths and credentials with your own:

```bash
docker run -d \
  --name youtube-to-navidrome \
  -p 5000:5000 \
  -v /path/to/your/music_library:/music/downloads \
  -v /path/to/navidrome/data/navidrome.db:/data/navidrome.db \
  -e SPOTIFY_CLIENT_ID="your_client_id_here" \
  -e SPOTIFY_CLIENT_SECRET="your_client_secret_here" \
  -e output_directory="/music/downloads" \
  -e NAVIDROME_DB_PATH="/data/navidrome.db" \
  -e MAX_WORKERS=2 \
  lifeadmin/youtube-to-navidrome:v1.0.0