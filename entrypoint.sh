#!/bin/sh
# Beende das Script sofort, falls ein Fehler auftritt
set -e

# 1. Updates beim Start ziehen
echo "Checking for updates..."
pip install --upgrade -r requirements.txt

# 2. Den Hauptprozess starten
# "exec" sorgt dafür, dass Flask die PID 1 übernimmt und Signale empfängt
echo "Starting Flask..."
exec flask run --host=0.0.0.0 --port=5000