FROM python:3.11-slim

WORKDIR /app

# System-Abhängigkeiten
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Pre-Install während des Builds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code kopieren
COPY . .

# Entrypoint-Script vorbereiten
RUN chmod +x entrypoint.sh

ENV FLASK_APP=app.py
EXPOSE 5000

# JSON-Syntax für ENTRYPOINT (Exec Form)
ENTRYPOINT ["./entrypoint.sh"]