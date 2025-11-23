import json

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, stream_with_context

from youtube_music_dl import process_input_url

load_dotenv()

app = Flask(__name__)
# No secret_key needed for this approach as we aren't using Flash anymore


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    # Get URL from the Javascript query parameters
    youtube_url = request.args.get("url")

    if not youtube_url:
        return (
            "data: "
            + json.dumps({"status": "error", "message": "No URL provided"})
            + "\n\n"
        )

    def generate():
        # We loop through your generator function
        for update_json in process_input_url(youtube_url):
            # SSE format requires "data: <message>\n\n"
            yield f"data: {update_json}\n\n"

    # stream_with_context keeps the request active while the loop runs
    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
