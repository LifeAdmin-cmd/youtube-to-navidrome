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

# Initialize the manager globally so both routes access the same instance
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
        # manager.process_url yields JSON strings directly
        # We wrap them in the SSE "data: ... \n\n" format
        for update_json in manager.process_url(youtube_url):
            yield f"data: {update_json}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/cancel", methods=["POST"])
def cancel():
    """Endpoint to trigger the cancellation event."""
    # We set the event flag inside the global manager instance
    manager.cancel_event.set()

    # Optional: You can explicitly call cleanup here,
    # though the workflow usually handles it when it catches the event.
    # manager.cleanup()

    return jsonify({"status": "ok", "message": "Cancellation signal sent."})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
