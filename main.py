# import os

# from dotenv import load_dotenv  # pip install python-dotenv

# # Import the coordinator function from your other file
# from youtube_music_dl import process_youtube_url

# # 1. Load Environment Variables (Spotify Keys)
# # This looks for a file named .env in the same folder
# load_dotenv()

# # Ensure keys are loaded
# if not os.getenv("SPOTIFY_CLIENT_ID"):
#     print("Error: SPOTIFY_CLIENT_ID is missing from environment or .env file")
#     exit(1)


# def main():
#     # 3. List the URLs you want to process
#     urls_to_process = [
#         "https://www.youtube.com/watch?v=EvNbcn7WfsE",  # SSIO
#         "https://youtu.be/YGU6cXgsy1Y?si=xDT_xPt9kGMQ2ncN",  # Miles McKenna
#         "https://youtu.be/dQw4w9WgXcQ?si=9d1kc0aj0ORWV4Pu",  # Rick Astley
#         "https://www.youtube.com/watch?v=wwux9KiBMjE",  # Ryan Gosling - I'm Just Ken
#         "https://youtu.be/7pLyutp2iJU?si=d8zrURLO8UMVAQcv",
#     ]

#     print(f"Processing {len(urls_to_process)} tracks...\n")

#     # 4. Loop through them
#     for url in urls_to_process:
#         try:
#             process_youtube_url(url)
#             print("\n" + "=" * 30 + "\n")
#         except Exception as e:
#             print(f"Skipping {url} due to error: {e}")


# if __name__ == "__main__":
#     main()
