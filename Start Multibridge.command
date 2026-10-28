#!/bin/bash
# Opens the Multibridge Pro 2 page in the browser once the local server answers.
cd "$(dirname "$0")"
URL="http://127.0.0.1:8765"

pause() {
  echo
  read -r -p "Press Return to close."
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required."
  echo "Install it from https://www.python.org/downloads/ or with: brew install python"
  pause
  exit 1
fi

(
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if curl -sf -o /dev/null "$URL"; then
      open "$URL"
      exit 0
    fi
    sleep 0.25
  done
) &

python3 mb.py serve
status=$?
if [ "$status" -ne 0 ]; then
  pause
fi
exit "$status"
