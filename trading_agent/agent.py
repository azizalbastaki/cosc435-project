"""Trading Agent for Ledger (COSC 435) - version 7.

One cycle (Student Development Guide 6.4):
  0. reconcile: an unresolved write, or our own PENDING/ESCALATED order  -> HOLD
  1. policy + quotas                                                      -> HOLD if exhausted
  2. theory validation (structure, ranges, pinned version)                -> HOLD if invalid
  3. eligible symbol = theory  AND  policy  AND  configured symbol         -> HOLD if none
  4. live quote (market_prices.php) + replay check against the public model
     (refresh the descriptor once on mismatch, then HOLD)
  5. price replay of the lookback window -> theory signal + Decimal bounds
  6. LANGUAGE MODEL decides inside the bounds (required); answer validated field by field
     -> model failure or invalid answer = HOLD
  7. one order validator guards the write
  8. persist intent -> propose once -> save transaction id; a lost response is an
     UncertainWrite: no further proposal until history reconciles it
A HOLD is a local decision and is never sent to Ledger.

Usage:
  python -m trading_agent.agent --config trading_agent/config.json --minutes 10   # one <=10-min session
  python -m trading_agent.agent --config ... --once [--dry-run]                    # one cycle
  python -m trading_agent.agent --config ... --reset-halt                          # human clears a halt
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import market_model as mm, money                          # noqa: E402
from common.ledger_client import (Cooldown, LedgerClient, LedgerError,  # noqa: E402
                                  UncertainWrite, startup_check)
from common.llm import ModelAdapter, ModelFailure                     # noqa: E402
from common.util import AuditLog, injection_hits, load_config, load_private_env  # noqa: E402
from trading_agent import model_decision, order_checks, strategy, theory_check  # noqa: E402


class State:
    def __init__(self, path: Path):
        self.path = path
        self.d = {"pinned_fingerprint": None, "own_orders": [], "trade_times": [],
                  "mismatch_streak": 0, "halted": None, "intent": None, "theory_snapshots": {}}
        if path.exists():
            self.d.update(json.loads(path.read_text()))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, default=str))
        tmp.replace(self.path)


class TradingAgent:
    def __init__(self, cfg: dict, client: LedgerClient | None = None, clock=time.time, log=None):
        self.cfg = cfg
        self.log = log or AuditLog(cfg.get("log_file", "logs/trading_agent.jsonl"))
        self.client = client or LedgerClient(cfg["base_url"], cfg["api_key"], log=self.log)
        self.clock = clock
        self.state = State(Path(cfg.get("state_file", "state/trading_agent_state.json")))
        self.llm = ModelAdapter(cfg.get("model_adapter", {}), log=self.log)
        self._model_cache: dict | None = None
        self._model_cache_at = 0.0

    # ------------------------------------------------------------------ human commands
    @property
    def reset_flag(self) -> Path:
        return self.state.path.with_name("reset_halt.flag")

    def _human_commands(self):
        if self.reset_flag.exists():
            self.log("halt_reset_by_human", previous=self.state.d.get("halted"), intent=self.state.d.get("intent"))
            self.state.d.update(halted=None, mismatch_streak=0, intent=None)
            self.reset_flag.unlink()
            self.state.save()

    # ------------------------------------------------------------------ one cycle
    def cycle(self, dry_run: bool = False) -> dict:
        self.log("flow_stage", stage="reconcile", detail="Read history and balances before deciding whether another proposal is safe.")
        self._human_commands()
        sym, now = self.cfg["symbol"], self.clock()
        st = self.state.d
        if st.get("halted"):
            return self._hold("halted", st["halted"])

        # 0. reconcile first: an unresolved write or our own open order blocks any new proposal
        txs = self.client.transactions(limit=100)
        port = self.client.portfolio()
        # one portfolio-value sample per cycle (every cycle_seconds) for return / max-drawdown reporting
        self.log("portfolio_sample", symbol=sym, total_value=port.get("total_value"), cash=port.get("cash_balance"),
                 holdings_value=port.get("holdings_value"))
        if st.get("intent"):
            if not self._reconcile_intent(txs):
                return self._hold("uncertain_write", "an earlier proposal's outcome is unknown and not found in history;"
                                  " check Transactions on the dashboard, then run --reset-halt")
        self._learn_outcomes(txs)
        own_ids = {o["id"] for o in st["own_orders"]}
        open_own = [t for t in txs if t["transaction_id"] in own_ids and t["status"] in ("PENDING", "ESCALATED")]
        if open_own:
            return self._hold("own_order_open", f"order(s) {[t['transaction_id'] for t in open_own]} still PENDING/ESCALATED")
        decided = [o for o in st["own_orders"] if o.get("final") in ("EXECUTED", "BLOCKED", "REJECTED")]
        if len(decided) >= 2 and all(o["final"] == "BLOCKED" for o in decided[-2:]):
            st["halted"] = "Guardian blocked two consecutive orders: waiting for a human to review and --reset-halt"
            self.log("ALERT", message=st["halted"])
            return self._hold("halted", st["halted"])
        if decided and decided[-1]["final"] == "BLOCKED" and now - decided[-1]["ts"] < self.cfg["checks"].get("block_cooldown_s", 900):
            return self._hold("guardian_blocked_cooldown", f"last order {decided[-1]['id']} was blocked; cooling down")

        # 1. policy and quotas
        policy = self.client.policy()
        lim = self.cfg["limits"]
        if policy.get("trades_last_hour", 0) >= min(lim["max_trades_per_hour"], policy.get("max_trades_per_hour", 1e9)) or \
                policy.get("trades_last_day", 0) >= min(lim["max_trades_per_day"], policy.get("max_trades_per_day", 1e9)):
            return self._hold("quota_exhausted", f"executed trades: {policy.get('trades_last_hour')}/h, "
                              f"{policy.get('trades_last_day')}/day")

        self.log("flow_stage", stage="theory", detail="Validate the submitted theory and allowed currency.")
        # 2. theory
        try:
            theory = theory_check.validate_theory(self.client.theory(), self.cfg)
        except theory_check.TheoryInvalid as exc:
            return self._hold("theory_invalid", str(exc))
        key = f"{theory['id']}:{theory['version']}"
        if key not in st["theory_snapshots"]:
            st["theory_snapshots"][key] = {k: theory[k] for k in theory}

        # 3. eligible symbol
        if sym not in theory["selected_symbols"] or sym not in policy.get("allowed_symbols", []):
            return self._hold("no_eligible_symbol", f"{sym} not selected by both theory and policy")

        # 4. live quote + replay check (one descriptor refresh, then HOLD)
        quote = self.client.quotes([sym]).get(sym)
        if not quote:
            return self._hold("no_quote", f"market_prices.php returned no {sym} quote")
        model = self._descriptor(force=False)
        if model.get("version") != quote.get("model_version") or not theory_check.replay_matches(model, sym, quote):
            model = self._descriptor(force=True)
            if not theory_check.replay_matches(model, sym, quote):
                return self._hold("replay_mismatch", f"replayed price {mm.quote(model, sym, quote['timestamp_unix'])} "
                                  f"!= live quote {quote['price']} after one refresh")
        try:
            fp = theory_check.validate_model(model, sym, st.get("pinned_fingerprint") or self.cfg.get("pinned_model_fingerprint"))
        except (theory_check.TheoryInvalid, mm.ModelError) as exc:
            return self._hold("market_data_untrusted", str(exc))
        if not st.get("pinned_fingerprint"):
            st["pinned_fingerprint"] = fp
            self.log("model_pinned", fingerprint=fp)

        self.log("flow_stage", stage="signals", detail="Replay historical prices, compute the signal, and bound the order size locally.")
        # 5. replay + signal + bounds
        t_now = quote["timestamp_unix"]
        lookback = int(theory["lookback_minutes"])
        if t_now - lookback * 60 < model["epoch_unix"]:
            return self._hold("insufficient_history", "lookback starts before the market epoch")
        samples = mm.minute_samples(model, sym, t_now, lookback)
        problems = theory_check.market_data_checks(samples, self.cfg["checks"]["max_5min_move_pct"])
        if problems:
            return self._hold("market_data_untrusted", "; ".join(problems))
        price = float(quote["price"])
        cash = float(port.get("cash_balance", 0))
        held, avg = self._position(port, txs, sym)
        sig = strategy.decide(price, samples, held, avg, cash, theory, lim,
                              start_price=mm.quote(model, sym, t_now - lookback * 60))
        bounds = self._bounds(price, cash, held, theory, policy)
        if sig.action == "BUY":
            sig.quantity = bounds["max_buy_quantity"]
        elif sig.action == "SELL":
            sig.quantity = bounds["max_sell_quantity"]
        if sig.action != "HOLD" and sig.quantity <= 0:
            sig.action, sig.reason = "HOLD", sig.reason + " (but the computed bound is zero)"

        # untrusted news: passed to the model as data only, scanned and logged
        try:
            news = [n for n in self.client.news() if n.get("symbol") in (sym, None, "")]
        except LedgerError:
            news = []
        if any(injection_hits(json.dumps(n)) for n in news):
            self.log("untrusted_news_flagged", count=sum(1 for n in news if injection_hits(json.dumps(n))))

        # 6. the model decides inside the bounds (required)
        payload = model_decision.build_payload(sym, quote, sig, bounds,
                                               {"cash": cash, "held": held, "avg_cost": avg}, policy, theory, news)
        try:
            raw = self.llm.ask_json(model_decision.SYSTEM_PROMPT, payload, model_decision.TRADER_SCHEMA, "trader_decision")
        except ModelFailure as exc:
            return self._hold("model_unavailable", f"{self.llm.label}: {exc}", sig.features)
        try:
            dec = model_decision.validate(raw, sym, sig, bounds)
        except model_decision.Invalid as exc:
            self.log("model_output_rejected", reason=str(exc), raw=str(raw)[:300])
            return self._hold("model_output_rejected", str(exc), sig.features)
        self.log("model_decision", model=self.llm.label, latency_s=self.llm.last_latency_s, rule=sig.action,
                 decision=dec["action"], quantity=str(dec["quantity"]), reasoning=dec["reasoning"][:600],
                 features=sig.features, bounds={k: str(v) for k, v in bounds.items()})
        if dec["action"] == "HOLD":
            return self._hold("model_hold" if sig.action != "HOLD" else "no_signal",
                              f"rules: {sig.reason}; model: {dec['reasoning'][:200]}", sig.features)
        if sig.kind == "stop_loss":
            st["halted"] = "stop loss hit: market no longer follows the theory; no new buys until a human resets"
            self.log("ALERT", message=st["halted"])

        self.log("flow_stage", stage="validate", detail="Check the model decision against theory, cash, holdings, and policy.")
        # 7. one validator guards the write
        qty = dec["quantity"]
        violations = order_checks.check_order(
            dec["action"], sym, qty, price, cfg=self.cfg, theory=theory, policy=policy, cash=cash, held_qty=held,
            recent_trade_times=st["trade_times"], now=now, open_own_orders=len(open_own))
        reasoning = (f"[theory {theory['id']} v{theory['version']} {theory['strategy']}] rules: {sig.reason}. "
                     f"Bounds: buy<={bounds['max_buy_quantity']} sell<={bounds['max_sell_quantity']} "
                     f"(cap ${lim['max_order_usd']:,}). Model {self.llm.label}: {dec['reasoning'][:600]}")
        if injection_hits(reasoning):
            violations.append("reasoning tripped the injection filter")
        if violations:
            return self._hold("local_check_failed", "; ".join(violations), sig.features)
        usd = money.cash_amount(qty, price, dec["action"])
        if dry_run:
            self.log("dry_run_proposal", action=dec["action"], qty=str(qty), usd=str(usd), reasoning=reasoning)
            self.state.save()
            return {"result": "dry_run", "action": dec["action"], "qty": qty}

        # 8. persist intent -> propose once -> save the id
        st["intent"] = {"ts": now, "symbol": sym, "action": dec["action"], "qty": money.qstr(qty),
                        "theory": key, "after_tx": max([t["transaction_id"] for t in txs], default=0)}
        self.state.save()
        try:
            resp = self.client.propose(sym, dec["action"], money.qstr(qty), reasoning, theory["id"], theory["version"])
        except UncertainWrite as exc:
            self.log("ALERT", message=f"uncertain write: {exc}. No further proposals until reconciled.")
            return {"result": "uncertain_write"}
        except Cooldown as exc:
            st["intent"] = None  # 429: the request was refused before processing, nothing was written
            return self._hold("cooldown", str(exc))
        except LedgerError as exc:
            st["intent"] = None  # 4xx: refused, nothing was written
            return self._hold("proposal_refused", f"{exc} {exc.payload}")
        tx_id = resp.get("transaction_id")
        st["intent"] = None
        st["own_orders"] = (st["own_orders"] + [{"id": tx_id, "ts": now, "action": dec["action"], "qty": money.qstr(qty),
                                                 "theory": key}])[-500:]
        st["trade_times"] = [t for t in st["trade_times"] if now - t < 86400] + [now]
        server_px = float(resp.get("market_price") or 0)
        dev = abs(server_px / price - 1) * 100 if server_px else 0.0
        if dev > self.cfg["checks"]["server_price_tolerance_pct"]:
            st["mismatch_streak"] += 1
            self.log("ALERT", message=f"server price {server_px} deviates {dev:.2f}% from quote {price}", streak=st["mismatch_streak"])
            if st["mismatch_streak"] >= 3:
                st["halted"] = "server price disagreed with the quote for 3 consecutive orders"
        else:
            st["mismatch_streak"] = 0
        self.log("proposed", tx=tx_id, action=dec["action"], qty=money.qstr(qty), usd=str(usd), status=resp.get("status"),
                 server_price=server_px, model=self.llm.label)
        self.state.save()
        return {"result": "proposed", "tx": tx_id, "action": dec["action"]}

    # ------------------------------------------------------------------ helpers
    def _descriptor(self, force: bool) -> dict:
        if force or self._model_cache is None or self.clock() - self._model_cache_at > 600:
            self._model_cache = self.client.market_model()
            mm.validate(self._model_cache)
            self._model_cache_at = self.clock()
        return self._model_cache

    def _bounds(self, price: float, cash: float, held: float, theory: dict, policy: dict) -> dict:
        lim, head = self.cfg["limits"], Decimal(str(1 - self.cfg["limits"].get("size_headroom_pct", 0.5) / 100))
        ceiling = min(money.D(lim["max_order_usd"]), money.D(policy.get("max_transaction_amount", lim["max_order_usd"])))
        budget = min(money.D(cash) * money.D(theory["position_fraction"]), ceiling * head, money.D(cash))
        if budget < money.D(lim.get("min_order_usd", 100)):
            budget = Decimal(0)
        max_buy = money.bounded_quantity(budget, price)
        max_sell = min(money.floor_qty(held), money.bounded_quantity(ceiling * head, price))
        return {"max_buy_budget_usd": budget.quantize(money.CENT), "max_buy_quantity": max_buy,
                "max_sell_quantity": max_sell, "transaction_ceiling_usd": ceiling}

    @staticmethod
    def _position(port: dict, txs: list, sym: str) -> tuple[float, float]:
        for h in port.get("holdings") or []:
            if h.get("symbol") == sym:
                return float(h.get("quantity") or 0), float(h.get("avg_cost") or 0)
        if port.get("holdings") == []:
            return 0.0, 0.0
        return strategy.avg_cost_from_history(txs, sym)

    def _reconcile_intent(self, txs: list) -> bool:
        """After an uncertain write: find the order in history, or keep holding."""
        it = self.state.d["intent"]
        for t in txs:
            if t["transaction_id"] > it.get("after_tx", 0) and t["symbol"] == it["symbol"] and t["action"] == it["action"] \
                    and abs(float(t.get("requested_quantity") or 0) - float(it["qty"])) < 1e-9:
                self.state.d["own_orders"].append({"id": t["transaction_id"], "ts": it["ts"], "action": it["action"],
                                                   "qty": it["qty"], "theory": it.get("theory")})
                self.log("intent_reconciled", tx=t["transaction_id"], status=t["status"])
                self.state.d["intent"] = None
                return True
        if self.clock() - it["ts"] > 120:
            self.log("ALERT", message="uncertain proposal not found in history after 2 min: human check needed")
        return False

    def _learn_outcomes(self, txs):
        own = {o["id"]: o for o in self.state.d["own_orders"]}
        for t in txs:
            o = own.get(t["transaction_id"])
            if o and o.get("final") != t["status"] and t["status"] != "PENDING":
                o["final"] = t["status"]
                self.log("order_outcome", tx=t["transaction_id"], status=t["status"],
                         guardian=t.get("guardian_decision"), reason=(t.get("guardian_reason") or "")[:200])

    def _hold(self, why: str, detail: str, features: dict | None = None) -> dict:
        self.log("hold", why=why, detail=detail, **({"features": features} if features else {}))
        self.state.save()
        return {"result": "hold", "why": why, "detail": detail}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "trading_agent" / "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--minutes", type=float, default=10, help="session length (course rule: at most 10)")
    ap.add_argument("--unlimited", action="store_true",
                    help="run until stopped (BREAKS the 10-minute course rule; logged as such)")
    ap.add_argument("--reset-halt", action="store_true", help="human acknowledgement: clear a halt / uncertain write")
    a = ap.parse_args(argv)
    load_private_env(ROOT, role="trader")
    cfg = load_config(a.config, "LEDGER_TRADER_KEY")
    agent = TradingAgent(cfg)
    if a.reset_halt:
        agent.reset_flag.parent.mkdir(parents=True, exist_ok=True)
        agent.reset_flag.write_text("reset by human\n")
        agent._human_commands()
        print("Halt reset applied.")
        return
    problem = startup_check(agent.client, "trader")
    if problem:
        sys.exit(problem)
    if not agent.llm.enabled:
        agent.log("ALERT", message="no language model configured: the agent will HOLD every cycle (course rule)")
    minutes = "unlimited" if a.unlimited else min(a.minutes, 10)
    end = float("inf") if a.unlimited else time.time() + minutes * 60
    if a.unlimited:
        agent.log("ALERT", message="UNLIMITED session: no time limit (exceeds the 10-minute course rule)")
    agent.log("session_start", minutes=minutes, symbol=cfg["symbol"], model=agent.llm.label)
    interval = max(60, cfg.get("cycle_seconds", 60))
    while True:
        try:
            agent.cycle(dry_run=a.dry_run)
        except Cooldown as exc:
            agent.log("cooldown", wait_s=round(exc.seconds, 1))
            time.sleep(min(exc.seconds, max(0, end - time.time())))
        except LedgerError as exc:
            agent.log("api_error", error=str(exc), payload=getattr(exc, "payload", None))
        except Exception as exc:  # isolate per-cycle errors; never crash the session
            agent.log("cycle_error", error=repr(exc)[:300])
        if a.once or time.time() + interval > end:
            break
        agent.log("flow_stage", stage="waiting", detail="Cycle complete. Waiting for the next scheduled check.", wait_s=interval)
        time.sleep(interval + random.uniform(0, 5))
    agent.log("session_end", model_calls=agent.llm.calls, model_failures=agent.llm.failures)


if __name__ == "__main__":
    main()
