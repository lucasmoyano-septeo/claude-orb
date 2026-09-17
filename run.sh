#!/usr/bin/env bash
# Lanza el botón flotante walkie-talkie. Evita duplicados.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if pgrep -f "$DIR/button.py" > /dev/null; then
  echo "Ya está corriendo."
  exit 0
fi
nohup "$DIR/venv/bin/python3" "$DIR/button.py" >> "$DIR/launch.log" 2>&1 &
disown
echo "Lanzado. Log en $DIR/assistant.log"
