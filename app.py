import os

from dotenv import load_dotenv
from flask import Flask, flash, render_template, request

from youtube_music_dl import process_youtube_url

# 1. Load Environment Variables
load_dotenv()

# Check keys (same as your script)
if not os.getenv("SPOTIFY_CLIENT_ID"):
    print("Error: SPOTIFY_CLIENT_ID is missing from environment or .env file")

app = Flask(__name__)
app.secret_key = "super_secret_key_for_flash_messages"  # Required for showing messages


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Get the URL from the form
        youtube_url = request.form.get("url")

        if not youtube_url:
            flash("Please enter a URL.", "error")
            return render_template("index.html")

        try:
            # 2. Process the URL
            print(f"Processing: {youtube_url}")  # Shows in your terminal
            process_youtube_url(youtube_url)

            # Send success message to the website
            flash(f"Successfully processed: {youtube_url}", "success")

        except Exception as e:
            # Send error message to the website
            flash(f"Error processing URL: {str(e)}", "error")

    return render_template("index.html")


if __name__ == "__main__":
    # debug=True allows the server to auto-reload if you change code
    app.run(debug=True, port=5000)
