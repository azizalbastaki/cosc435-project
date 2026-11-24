#!/usr/bin/env bash
# macOS/Linux convenience: run ONE session (<= 10 min) in the background via run_session.py.
#   bash start.sh              # 10 minutes
#   MINUTES=5 bash start.sh
# Windows (or to watch it live): python run_session.py
set -euo pipefail
cd "$(dirname "$0")"
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 not found" >&2; exit 1; }
mkdir -p logs state
if [ -f state/pids ] && kill -0 "$(head -1 state/pids)" 2>/dev/null; then
  echo "A session is still running. Wait for it to finish or run: bash stop.sh" >&2; exit 1
fi
nohup "$PY" run_session.py --minutes "${MINUTES:-10}" > logs/session.out 2>&1 &
echo $! > state/pids
sleep 8
cat logs/session.out
if ! kill -0 "$(head -1 state/pids)" 2>/dev/null; then echo "Session did not start (see above)." >&2; exit 1; fi
echo "Running in the background. Status: bash status.sh   Stop early: bash stop.sh"
