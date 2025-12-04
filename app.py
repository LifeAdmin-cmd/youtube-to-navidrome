from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

from workflow import WorkflowManager

load_dotenv()

app = Flask(__name__)

# Initialize the manager globally
manager = WorkflowManager()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def get_state():
    """Returns full current state for UI restoration."""
    state = manager.get_full_state()
    # Convert Path objects to strings for JSON serialization if necessary
    # (manager.get_full_state returns serializable tracks, but paths inside tracks need handling if not done yet)
    # The _save_state logic handled paths, but self.tracks has Path objects in memory.

    # Quick fix for serialization for the frontend
    serializable_tracks = []
    for uid, track in state["tracks"].items():
        t = track.copy()
        if t.get("path"):
            t["path"] = str(t["path"])
        t["track_uid"] = uid  # Ensure UID is in the object
        serializable_tracks.append(t)

    state["tracks"] = serializable_tracks
    return jsonify(state)


@app.route("/start", methods=["POST"])
def start_process():
    """Starts the download workflow in the background."""
    data = request.json
    youtube_url = data.get("url")
    if not youtube_url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    try:
        manager.start_processing(youtube_url)
        return jsonify({"status": "ok", "message": "Started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/start_retry", methods=["POST"])
def start_retry_process():
    try:
        manager.start_retry()
        return jsonify({"status": "ok", "message": "Retry started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/stream")
def stream():
    """SSE endpoint that clients subscribe to."""

    def generate():
        for msg in manager.subscribe():
            yield msg

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/cancel", methods=["POST"])
def cancel():
    manager.cancel_event.set()
    return jsonify({"status": "ok", "message": "Cancellation signal sent."})


@app.route("/api/candidates/<track_uid>", methods=["GET"])
def get_candidates(track_uid):
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
    try:
        count = manager.delete_all_failed_tracks()
        return jsonify({"status": "ok", "count": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/rerun_search/<track_uid>", methods=["POST"])
def rerun_search(track_uid):
    data = request.json
    custom_query = data.get("query")
    if not track_uid:
        return jsonify({"error": "Missing track_uid"}), 400
    try:
        # Renamed variable locally to match new logic
        result = manager.rerun_spotify_search(track_uid, custom_query)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# NOTE: /api/failed_tracks is no longer strictly necessary if /api/state returns everything,
# but we can keep it for backwards compat or specialized views if needed.
# For now, UI will likely use /api/state.

if __name__ == "__main__":
    app.run(debug=True, port=5000)
