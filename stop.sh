#!/usr/bin/env bash
# Stop the running session started by start.sh (both programs stop cleanly).
cd "$(dirname "$0")"
[ -f state/pids ] || { echo "Nothing running."; exit 0; }
while read -r pid; do kill "$pid" 2>/dev/null && echo "stopped $pid"; done < state/pids
rm -f state/pids
