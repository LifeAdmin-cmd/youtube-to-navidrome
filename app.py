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

# Import the cancel function along with the processor
from youtube_music_dl import process_input_url, trigger_cancellation

load_dotenv()

app = Flask(__name__)


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
        # The generator now yields "cancelled" status if interrupted
        for update_json in process_input_url(youtube_url):
            yield f"data: {update_json}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# --- NEW ROUTE ---
@app.route("/cancel", methods=["POST"])
def cancel():
    """Endpoint to trigger the cancellation event."""
    trigger_cancellation()
    return jsonify({"status": "ok", "message": "Cancellation signal sent."})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
