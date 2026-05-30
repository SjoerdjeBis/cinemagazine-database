#!/bin/bash
cd "$(dirname "$0")"

# Controleer of poort 3456 al in gebruik is
if lsof -Pi :3456 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "Server draait al op poort 3456."
else
  echo "Server starten op http://localhost:3456 ..."
  python3 -m http.server 3456 &
  sleep 0.5
fi

# Open de browser
open http://localhost:3456

echo "Klaar! Sluit dit venster niet — de server stopt dan ook."
wait
