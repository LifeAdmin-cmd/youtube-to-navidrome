import json

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

# Import the Class from your new workflow file
from workflow import WorkflowManager

load_dotenv()

app = Flask(__name__)

# Initialize the manager globally
manager = WorkflowManager()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    youtube_url = request.args.get("url")

    if not youtube_url:
        return (
            "data: "
            + json.dumps({"status": "error", "message": "No URL provided"})
            + "\n\n"
        )

    def generate():
        for update_json in manager.process_url(youtube_url):
            yield f"data: {update_json}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/stream_retry")
def stream_retry():
    """Stream endpoint for retrying all failed tracks."""

    def generate():
        for update_json in manager.retry_failed_tracks():
            yield f"data: {update_json}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/cancel", methods=["POST"])
def cancel():
    manager.cancel_event.set()
    return jsonify({"status": "ok", "message": "Cancellation signal sent."})


@app.route("/api/candidates/<track_uid>", methods=["GET"])
def get_candidates(track_uid):
    """Returns the list of Spotify candidates for a specific download, plus track info for the modal."""
    track = manager.tracks.get(track_uid)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    youtube_title = track["youtube_title"]
    uploader = track["video_info"].get("uploader")
    original_query = f"{youtube_title} - {uploader}" if uploader else youtube_title

    return jsonify(
        {
            "candidates": track["candidates"],
            "youtube_title": youtube_title,
            "original_query": original_query,
        }
    )


@app.route("/api/update_tag", methods=["POST"])
def update_tag():
    """Updates the tags for a file using a new Spotify ID."""
    data = request.json
    track_uid = data.get("track_uid")
    spotify_id = data.get("spotify_id")

    if not track_uid or not spotify_id:
        return jsonify({"error": "Missing parameters"}), 400

    try:
        result = manager.update_track_tags(track_uid, spotify_id)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/delete_track", methods=["POST"])
def delete_track():
    """Deletes the downloaded file associated with the track_uid."""
    data = request.json
    track_uid = data.get("track_uid")

    if not track_uid:
        return jsonify({"error": "Missing track_uid"}), 400

    try:
        manager.delete_track(track_uid)
        return jsonify({"status": "ok", "message": "File deleted."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/delete_all_failed", methods=["POST"])
def delete_all_failed():
    """Deletes all tracks marked as 'error'."""
    try:
        count = manager.delete_all_failed_tracks()
        return jsonify({"status": "ok", "count": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/rerun_search/<track_uid>", methods=["POST"])
def rerun_search(track_uid):
    """Performs a new Spotify search for the track using a custom or original query."""
    data = request.json
    custom_query = data.get("query")

    if not track_uid:
        return jsonify({"error": "Missing track_uid"}), 400

    try:
        result = manager.rerun_spotify_search(track_uid, custom_query)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/failed_tracks", methods=["GET"])
def get_failed_tracks():
    """Returns a list of tracks that failed or were skipped, for display on page load."""
    failed_tracks = []

    for uid, track in manager.tracks.items():
        # if track["status"] in ["error", "skipped"]:
        if track["status"] in ["error"]:

            tags = track.get("best_match_tags") or {}

            # The structure needed matches the stream event data
            failed_tracks.append(
                {
                    "track_uid": uid,
                    "youtube_title": track["youtube_title"],
                    # Use saved best match tags (present on skipped, None on error)
                    "spotify_title": tags.get("Title"),
                    "spotify_artist": tags.get("Artist"),
                    "status": track["status"],
                }
            )

    return jsonify({"tracks": failed_tracks})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
