"""Run ONE trading session (course rule: at most 10 minutes) - Windows, macOS and Linux.

  python3 run_session.py              # 10 minutes, Guardian first, then the Trading Agent
  python3 run_session.py --minutes 5
Ctrl+C stops both programs. Each session is appended to logs/sessions.csv (start, end, coin, theory
version, model, fills, Guardian decisions) for the report. Keys are read from .env.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common.util import load_private_env  # noqa: E402

FIELDS = ["session_start_utc", "session_end_utc", "minutes", "member", "symbol", "theory_version", "model",
          "proposals", "guardian_reviews", "approve", "modify", "block", "escalate", "model_failures",
          "agent_exit", "guardian_exit"]


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarise(start: str, end: str) -> dict:
    """Count what happened in this session from the local audit logs."""
    out = {k: 0 for k in ("proposals", "guardian_reviews", "approve", "modify", "block", "escalate", "model_failures")}
    for name in ("trading_agent", "guardian"):
        p = ROOT / "logs" / f"{name}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("ts", "")[:19] + "Z"
            if not (start <= ts <= end):
                continue
            ev = e.get("event")
            if ev == "proposed":
                out["proposals"] += 1
            elif ev == "review":
                out["guardian_reviews"] += 1
                d = str(e.get("decision", "")).lower()
                if d in out:
                    out[d] += 1
            elif ev in ("model_failure", "model_output_rejected", "model_unavailable"):
                out["model_failures"] += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--unlimited", action="store_true",
                    help="run until stopped (BREAKS the 10-minute course rule; recorded in sessions.csv)")
    a = ap.parse_args()
    if not a.unlimited and (a.minutes > 10 or a.minutes <= 0):
        sys.exit("Sessions are limited to 10 minutes by the course rules.")
    load_private_env(ROOT)
    cfg_a, cfg_g = ROOT / "trading_agent" / "config.json", ROOT / "guardian" / "config.json"
    if not (cfg_a.exists() and cfg_g.exists()):
        sys.exit("Not configured yet: run  python3 setup.py")
    ca, cg = json.loads(cfg_a.read_text()), json.loads(cfg_g.read_text())
    key_env = cg["model_adapter"].get("api_key_env", "MODEL_API_KEY")
    for k in ("LEDGER_TRADER_KEY", "LEDGER_GUARDIAN_KEY", key_env):
        if not os.environ.get(k):
            sys.exit(f"{k} is empty in .env: open the .env file in this folder, paste your key after {k}=, save, retry.")
    if os.environ["LEDGER_TRADER_KEY"] == os.environ["LEDGER_GUARDIAN_KEY"]:
        sys.exit("Trader and guardian must use different keys.")
    if not a.skip_tests:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT,
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit("Self-tests failed; not starting:\n" + r.stderr[-1500:])
        print("Self-tests OK.")
    (ROOT / "logs").mkdir(exist_ok=True)
    start = now_iso()
    deadline = float("inf") if a.unlimited else time.monotonic() + a.minutes * 60
    children = []
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    mins = "unlimited" if a.unlimited else str(a.minutes)
    length_args = ["--unlimited"] if a.unlimited else ["--minutes", mins]
    py = sys.executable
    os.environ["LEDGER_RUN_ID"] = uuid.uuid4().hex[:12]
    guardian_env = dict(os.environ)
    guardian_env.pop("LEDGER_TRADER_KEY", None)
    trader_env = dict(os.environ)
    trader_env.pop("LEDGER_GUARDIAN_KEY", None)
    g = t = awake = None
    with (ROOT / "logs/guardian.out").open("a") as gout, (ROOT / "logs/trading_agent.out").open("a") as aout:
        try:
            if not stopped:
                g = subprocess.Popen([py, "-m", "guardian.guardian", "--config", str(cfg_g), *length_args],
                                     cwd=ROOT, env=guardian_env, stdout=gout, stderr=subprocess.STDOUT)
                children.append(g)
            time.sleep(3)
            if not stopped and g is not None and g.poll() is None:
                t = subprocess.Popen([py, "-m", "trading_agent.agent", "--config", str(cfg_a), *length_args],
                                     cwd=ROOT, env=trader_env, stdout=aout, stderr=subprocess.STDOUT)
                children.append(t)
            elif not stopped:
                stop()
            if shutil.which("caffeinate") and g is not None and not stopped:
                awake = subprocess.Popen(["caffeinate", "-ims", "-w", str(g.pid)])
            print(f"Session started {start} for {mins if a.unlimited else mins + ' min'}: {ca['symbol']}. Ctrl+C stops both agents.")
            while not stopped and any(p.poll() is None for p in children):
                if time.monotonic() >= deadline:
                    print("Session time limit reached. Stopping both agents.")
                    break
                if any(p.poll() not in (None, 0) for p in children) or (g is not None and g.poll() is not None and t is not None and t.poll() is None):
                    print("Reviewer stopped or an agent failed. Stopping both agents.")
                    break
                time.sleep(0.25 if a.unlimited else min(0.25, max(0, deadline - time.monotonic())))
        finally:
            stop()
            for p in children:
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            if awake and awake.poll() is None:
                awake.terminate()
    end = now_iso()
    s = summarise(start, end)
    row = {"session_start_utc": start, "session_end_utc": end, "minutes": mins, "member": ca.get("member", ""),
           "symbol": ca["symbol"], "theory_version": ca["expected_theory"]["version"],
           "model": f"{ca['model_adapter']['provider']}:{ca['model_adapter']['model']}", **s,
           "agent_exit": t.returncode if t else "not_started", "guardian_exit": g.returncode if g else "not_started"}
    path = ROOT / "logs" / "sessions.csv"
    new = not path.exists() or path.read_text().splitlines()[:1] != [",".join(FIELDS)]
    if new and path.exists():
        path.rename(path.with_suffix(".old.csv"))
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"Session ended {end}: {s['proposals']} proposal(s); Guardian reviews {s['guardian_reviews']} "
          f"(APPROVE {s['approve']}, MODIFY {s['modify']}, BLOCK {s['block']}, ESCALATE {s['escalate']}); "
          f"model failures {s['model_failures']}. Recorded in logs/sessions.csv.")
    for name, p in (("Guardian", g), ("Trading Agent", t)):
        if p is not None and p.returncode not in (0, None, -15):
            out = (ROOT / "logs" / ("guardian.out" if name == "Guardian" else "trading_agent.out")).read_text(errors="replace")
            print(f"WARNING: {name} stopped with an error: {out.strip().splitlines()[-1] if out.strip() else 'see logs'}")
    print("Results so far: python3 tools/session_report.py")
    if any(p is not None and p.returncode not in (0, None, -15) for p in (t, g)):
        sys.exit(1)


if __name__ == "__main__":
    main()
