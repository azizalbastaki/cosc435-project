"""Results for the report, for ONE member's account and coin (guide 8.3). Read-only: GETs only.

  python3 tools/session_report.py                    # writes reports/<COIN>_<member>.md and trades CSV
  python3 tools/session_report.py --start-value 100000

Combines Ledger (transactions, Guardian Log, portfolio, market model) with the local audit logs
(sessions.csv, portfolio samples, model failures). Computes completed fills, Guardian decisions,
portfolio return, maximum drawdown (and how often value was sampled), and cash / buy-and-hold benchmarks.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import market_model as mm                       # noqa: E402
from common.ledger_client import LedgerClient                # noqa: E402
from common.util import load_private_env                     # noqa: E402


def _ts(s: str) -> float:
    s = s.replace("T", " ").replace("Z", "")[:19]
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def local_events(name: str):
    p = ROOT / "logs" / f"{name}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def build_report(trader, guardian, cfg: dict, start_value: float, now: float) -> tuple[str, list[dict]]:
    sym, member = cfg["symbol"], cfg.get("member") or "member"
    txs = [t for t in trader.transactions(limit=200) if t.get("symbol") == sym]
    port = trader.portfolio()
    glogs = [g for g in guardian.guardian_logs(limit=200) if g.get("symbol", sym) == sym]
    model = trader.market_model()
    fills = sorted([t for t in txs if t["status"] == "EXECUTED" and t["action"] in ("BUY", "SELL")
                    and t.get("executed_quantity")], key=lambda t: t["transaction_id"])
    by_status = collections.Counter(t["status"] for t in txs if t["action"] != "HOLD")
    decisions = collections.Counter(g["decision"] for g in glogs)
    rules = collections.Counter(r for g in glogs for r in (g.get("rules_triggered") or []))
    # portfolio value samples (one per agent cycle) + the current value
    samples = [(_ts(e["ts"]), float(e["total_value"])) for e in local_events("trading_agent")
               if e.get("event") == "portfolio_sample" and e.get("total_value") is not None]
    samples.append((now, float(port.get("total_value") or 0)))
    samples.sort()
    peak, mdd = samples[0][1], 0.0
    for _, v in samples:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak if peak else 0)
    gaps = [b[0] - a[0] for a, b in zip(samples, samples[1:]) if b[0] > a[0]]
    freq = f"every ~{statistics.median(gaps)/60:.0f} min during sessions" if gaps else "single sample"
    end_value = samples[-1][1]
    ret = end_value / start_value - 1
    t0 = min([_ts(t["created_at"]) for t in fills] + [samples[0][0]])
    bh = mm.quote(model, sym, now) / mm.quote(model, sym, t0) - 1
    model_events = collections.Counter(e["event"] for e in local_events("trading_agent") + local_events("guardian")
                                       if e.get("event") in ("model_failure", "model_output_rejected", "model_unavailable",
                                                             "model_decision", "model_judgment"))
    sessions = []
    sp = ROOT / "logs" / "sessions.csv"
    if sp.exists():
        sessions = list(csv.DictReader(sp.open()))
    unresolved = [t for t in txs if t["status"] in ("PENDING", "ESCALATED")]

    L = [f"# Results - {sym} ({member})", "",
         f"Generated {_iso(now)} from Ledger (read-only) and local audit logs. Observation period {_iso(t0)} - {_iso(now)}.", "",
         "## Portfolio", "",
         "| Metric | Value |", "|---|---|",
         f"| Starting value | ${start_value:,.2f} |", f"| Ending value | ${end_value:,.2f} |",
         f"| Portfolio return | {ret*100:+.2f}% |", f"| Maximum drawdown | {mdd*100:.2f}% |",
         f"| Value sampled | {len(samples)} samples, {freq} |",
         "| Benchmark: keep cash | +0.00% |", f"| Benchmark: buy-and-hold {sym} over the same period | {bh*100:+.2f}% |",
         f"| Cash / holdings now | ${float(port.get('cash_balance', 0)):,.2f} / ${float(port.get('holdings_value', 0)):,.2f} |", "",
         "## Orders and Guardian decisions", "",
         f"Completed fills (EXECUTED BUY/SELL): **{len(fills)}** "
         f"({sum(t['action'] == 'BUY' for t in fills)} BUY, {sum(t['action'] == 'SELL' for t in fills)} SELL).", "",
         "| Ledger status | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(by_status.items())] + [
         "", "| Guardian decision | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(decisions.items())] + [
         "", "Most frequent rules triggered: " + (", ".join(f"{k} ({v})" for k, v in rules.most_common(8)) or "none"), "",
         "Model activity (local logs): " + (", ".join(f"{k} {v}" for k, v in sorted(model_events.items())) or "none"), "",
         f"Unresolved orders (PENDING/ESCALATED): {', '.join(str(t['transaction_id']) for t in unresolved) or 'none'}", "",
         "## Sessions (each at most 10 minutes)", ""]
    if sessions:
        keys = ["session_start_utc", "session_end_utc", "theory_version", "model", "proposals", "approve", "modify",
                "block", "escalate", "model_failures"]
        L += ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
        L += ["| " + " | ".join(str(s.get(k, "")) for k in keys) + " |" for s in sessions]
    else:
        L.append("No sessions recorded in logs/sessions.csv (run sessions with run_session.py or start.sh).")
    L += ["", "## Completed fills", "", "| ID | Time (UTC) | Action | Quantity | Price | Total | Guardian | Risk |", "|---|---|---|---|---|---|---|---|"]
    rows = []
    for t in fills:
        L.append(f"| {t['transaction_id']} | {t.get('executed_at') or t['created_at']} | {t['action']} | "
                 f"{float(t['executed_quantity']):.6f} | {float(t['executed_price']):,.6f} | "
                 f"${float(t['executed_total']):,.2f} | {t.get('guardian_decision')} | {t.get('risk_score')} |")
        rows.append({k: t.get(k) for k in ("transaction_id", "created_at", "executed_at", "action", "executed_quantity",
                                            "executed_price", "executed_total", "guardian_decision", "risk_score", "theory_version")})
    L += ["", "Note: prices come from Ledger's public deterministic model (ledger-powerlaw-v1); gains reflect the "
          "strategy exploiting its cycles, not real-world forecasting skill."]
    return "\n".join(L) + "\n", rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-value", type=float, default=100000.0)
    a = ap.parse_args()
    load_private_env(ROOT)
    import os
    cfg = json.loads((ROOT / "trading_agent" / "config.json").read_text())
    base = cfg["base_url"]
    trader = LedgerClient(base, os.environ.get("LEDGER_TRADER_KEY", ""))
    guardian = LedgerClient(base, os.environ.get("LEDGER_GUARDIAN_KEY", ""))
    md, rows = build_report(trader, guardian, cfg, a.start_value, dt.datetime.now(dt.timezone.utc).timestamp())
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    name = f"{cfg['symbol']}_{(cfg.get('member') or 'member').replace(' ', '_')}"
    (out / f"{name}.md").write_text(md)
    with (out / f"{name}_fills.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["transaction_id"])
        w.writeheader()
        w.writerows(rows)
    print(md)
    print(f"Saved reports/{name}.md and reports/{name}_fills.csv")


if __name__ == "__main__":
    main()
