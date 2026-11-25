"""Price replay: validate the theory by re-running the exact live decision code over
past (or future) prices of Ledger's deterministic market.

It uses the same `strategy.decide` and `order_checks.check_order` functions as the
live agent, simulates the platform's execution at the model price, and reports
return, buy-and-hold, drawdown, trade counts and win rate.

Usage (offline, no API key needed when --model-file is given):
  python -m trading_agent.replay --model-file data/market_model.json \
      --start 2026-09-01T00:00 --end 2026-11-22T12:00 --theory-file data/theory_v1.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import market_model as mm          # noqa: E402
from trading_agent import order_checks, strategy, theory_check  # noqa: E402

DEFAULT_CFG = {"symbol": "SOL", "limits": {"max_order_usd": 9000, "min_order_usd": 100,
                                          "max_trades_per_hour": 4, "max_trades_per_day": 24}}
DEFAULT_POLICY = {"max_transaction_amount": 10000, "restricted_actions": ["WITHDRAW"],
                  "allowed_symbols": ["SOL"], "max_trades_per_hour": 10, "max_trades_per_day": 50}


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp())


def replay(model: dict, theory: dict, start: int, end: int, cfg: dict = DEFAULT_CFG,
           policy: dict = DEFAULT_POLICY, step_s: int = 300, cash0: float = 100_000.0) -> dict:
    sym = cfg["symbol"]
    theory_check.validate_theory(theory, dict(cfg, expected_theory=cfg.get("expected_theory", {})))
    mm.validate(model)
    lb = int(theory["lookback_minutes"])
    if start >= end or step_s <= 0 or step_s % 60 or start - lb * 60 < model["epoch_unix"]:
        raise ValueError("replay needs sufficient history, start < end, and a positive whole-minute step")
    # 1-minute price samples from (start - lookback) to end
    t0 = start - lb * 60
    prices = [mm.quote(model, sym, t0 + 60 * i) for i in range((end - t0) // 60 + 1)]
    csum = [0.0]
    for p in prices:
        csum.append(csum[-1] + p)
    cash, qty, cost = cash0, 0.0, 0.0
    times, trades, wins, sells, kinds = [], [], 0, 0, {}
    peak, mdd = cash0, 0.0
    for now in range(start, end, step_s):
        i = (now - t0) // 60
        price = prices[i]
        mean = (csum[i + 1] - csum[i + 1 - lb]) / lb
        sig = strategy.decide(price, None, qty, cost / qty if qty > 1e-12 else 0, cash, theory,
                              cfg["limits"], mean=mean, start_price=prices[i - lb])
        times = [t for t in times if now - t < 86400]
        if sig.action != "HOLD":
            pol = dict(policy, trades_last_hour=sum(1 for t in times if now - t < 3600),
                       trades_last_day=len(times))
            v = order_checks.check_order(sig.action, sym, sig.quantity, price, cfg=cfg, theory=theory,
                                         policy=pol, cash=cash, held_qty=qty, recent_trade_times=times,
                                         now=now, open_own_orders=0)
            if not v:  # Guardian approves compliant orders; platform fills at the model price
                usd = sig.quantity * price
                if sig.action == "BUY":
                    cash -= usd; qty += sig.quantity; cost += usd
                else:
                    basis = cost * sig.quantity / qty
                    sells += 1; wins += usd > basis
                    cost -= basis; qty -= sig.quantity; cash += usd
                    if qty < 1e-9:
                        qty, cost = 0.0, 0.0
                times.append(now)
                trades.append((now, sig.action, sig.kind))
                kinds[sig.kind] = kinds.get(sig.kind, 0) + 1
        val = cash + qty * price
        peak = max(peak, val)
        mdd = max(mdd, 1 - val / peak)
    last = prices[(end - t0) // 60]
    final = cash + qty * last
    days = (end - start) / 86400
    first = prices[(start - t0) // 60]
    return {"start": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
            "end": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat(), "days": round(days, 2),
            "final_value": round(final, 2), "return_pct": round((final / cash0 - 1) * 100, 2),
            "buy_hold_pct": round((last / first - 1) * 100, 2), "max_drawdown_pct": round(mdd * 100, 2),
            "trades": len(trades), "trades_per_day": round(len(trades) / days, 1),
            "sells": sells, "profitable_sells_pct": round(100 * wins / sells, 1) if sells else None,
            "exits_by_kind": kinds, "open_position_qty": round(qty, 6)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-file", required=True, help="JSON from /api/v1/market_model.php (the 'model' object)")
    ap.add_argument("--theory-file", required=True, help="JSON from /api/v1/theory.php (the 'theory' object)")
    ap.add_argument("--symbol", choices=theory_check.SUPPORTED, default="SOL")
    ap.add_argument("--start", required=True, help="UTC ISO time, e.g. 2026-09-01T00:00")
    ap.add_argument("--end", required=True)
    ap.add_argument("--step-seconds", type=int, default=300)
    a = ap.parse_args(argv)
    model = json.loads(Path(a.model_file).read_text())
    model = model.get("model", model)
    theory = json.loads(Path(a.theory_file).read_text())
    theory = theory.get("theory", theory)
    mm.validate(model)
    print(json.dumps(replay(model, theory, _ts(a.start), _ts(a.end), cfg=dict(DEFAULT_CFG, symbol=a.symbol),
                            policy=dict(DEFAULT_POLICY, allowed_symbols=[a.symbol]), step_s=a.step_seconds), indent=1))


if __name__ == "__main__":
    main()
