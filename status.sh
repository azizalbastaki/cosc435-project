#!/usr/bin/env bash
# Show whether both programs are running and their latest decisions.
cd "$(dirname "$0")"
names=("Session (Guardian + Trading Agent)")
i=0
if [ -f state/pids ]; then
  while read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then s="running"; else s="STOPPED"; fi
    echo "${names[$i]:-proc} (pid $pid): $s"; i=$((i+1))
  done < state/pids
else
  echo "Not started (run ./start.sh)"
fi
echo; echo "--- Guardian (last 5) ---"; tail -n 5 logs/guardian.jsonl 2>/dev/null | cut -c1-300
echo; echo "--- Trading Agent (last 5) ---"; tail -n 5 logs/trading_agent.jsonl 2>/dev/null | cut -c1-300
echo; echo "--- Errors ---"; grep -h -E '"event": "(api_error|guardian_error|ALERT|review_failed)"' logs/*.jsonl 2>/dev/null | tail -n 5 | cut -c1-300
tail -n 3 logs/guardian.out logs/trading_agent.out 2>/dev/null | grep -i -E 'error|traceback' || true
