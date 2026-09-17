#!/usr/bin/env bash
# Launches the floating walkie-talkie button. Avoids duplicates.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if pgrep -f "$DIR/button.py" > /dev/null; then
  echo "Already running."
  exit 0
fi
nohup "$DIR/venv/bin/python3" "$DIR/button.py" >> "$DIR/launch.log" 2>&1 &
disown
echo "Launched. Log at $DIR/assistant.log"
