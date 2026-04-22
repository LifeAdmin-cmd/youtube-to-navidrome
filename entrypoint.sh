#!/bin/sh
# Beende das Script sofort, falls ein Fehler auftritt
set -e

# 1. Updates beim Start ziehen
echo "Checking for updates..."
pip install --upgrade -r requirements.txt

# 2. Den Hauptprozess starten über Gunicorn (WSGI)
echo "Starting Gunicorn WSGI Server..."
# At least 1 worker is REQUIRED because the app uses an in-memory WorkflowManager state
exec gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 app:app