# Use a lightweight Python base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system-level dependencies (FFmpeg is required for audio processing)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set Flask environment variables
ENV FLASK_APP=app.py

# Expose the default Flask port (internal)
EXPOSE 5000

# Run Flask binding to 0.0.0.0 so it is accessible externally
CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]