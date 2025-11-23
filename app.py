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


@app.route("/cancel", methods=["POST"])
def cancel():
    manager.cancel_event.set()
    return jsonify({"status": "ok", "message": "Cancellation signal sent."})


@app.route("/api/candidates/<track_uid>", methods=["GET"])
def get_candidates(track_uid):
    """Returns the list of Spotify candidates for a specific download."""
    track = manager.tracks.get(track_uid)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    return jsonify({"candidates": track["candidates"]})


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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
