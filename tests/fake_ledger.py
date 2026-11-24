"""In-memory stand-in for the Ledger API, faithful to the shapes in the Student Development Guide
(Appendix B). Executes approved/modified orders at the current quote so whole sessions can be simulated.
`fail_next_review` / `fail_next_propose` simulate a lost response AFTER the server applied the write."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from common import market_model as mm

DATA = Path(__file__).resolve().parents[1] / "data"


def fmt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class FakeLedger:
    def __init__(self, now: float, cash: float = 100_000.0):
        self.now = now
        self.model = json.loads((DATA / "market_model.json").read_text())["model"]
        self.theory_obj = json.loads((DATA / "theory_v1.json").read_text())["theory"]
        self.cash = cash
        self.holdings: dict[str, float] = {}
        self.txs: list[dict] = []
        self.news_items: list[dict] = []
        self.policy_obj = {"max_transaction_amount": 10000, "max_trades_per_hour": 10, "max_trades_per_day": 50,
                           "restricted_actions": ["WITHDRAW"], "escalation_threshold": 50000,
                           "allowed_symbols": ["BTC", "ETH", "DOGE", "XMR", "XRP", "SOL"]}
        self.price_override: float | None = None
        self.reviews: list[dict] = []
        self.next_id = 1000
        self.fail_next_review = False
        self.fail_next_propose = False
        self.calls: list[str] = []

    # --- helpers -----------------------------------------------------------------
    def price(self, sym):
        return self.price_override or mm.quote(self.model, sym, self.now)

    def _executed_recent(self, window):
        return [t for t in self.txs if t["status"] == "EXECUTED" and t["action"] in ("BUY", "SELL")
                and self.now - dt.datetime.strptime(t["executed_at"], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=dt.timezone.utc).timestamp() < window]

    def inject(self, action, qty, sym="SOL", label="sol-trader", reasoning="", price=None, theory=True):
        """Create a pending order directly (what the attack simulator does)."""
        px = price or self.price(sym)
        self.next_id += 1
        t = {"transaction_id": self.next_id, "theory_id": self.theory_obj["id"] if theory else None,
             "theory_version": self.theory_obj["version"] if theory else None, "symbol": sym, "action": action,
             "requested_quantity": qty, "requested_price": px, "status": "PENDING", "executed_quantity": None,
             "executed_price": None, "executed_total": None, "reasoning": reasoning, "guardian_decision": None,
             "guardian_reason": None, "risk_score": None, "created_at": fmt(self.now), "reviewed_at": None,
             "executed_at": None, "_label": label}
        self.txs.append(t)
        return t

    # --- API surface (same method names as LedgerClient) ------------------------
    def market_model(self):
        return dict(self.model, server_time_unix=int(self.now), current_tick=mm.tick_at(self.model, self.now))

    def quotes(self, symbols):
        tick = mm.tick_at(self.model, self.now)
        ts = self.model["epoch_unix"] + tick * self.model["tick_seconds"]
        return {s: {"symbol": s, "price": self.price(s), "timestamp_unix": ts, "tick": tick,
                    "model_version": self.model["version"]} for s in symbols}

    def portfolio(self):
        h = []
        for s, q in self.holdings.items():
            if q > 1e-12:
                bq = sum(float(t["executed_quantity"]) for t in self.txs if t["symbol"] == s and t["status"] == "EXECUTED" and t["action"] == "BUY")
                bc = sum(float(t["executed_total"]) for t in self.txs if t["symbol"] == s and t["status"] == "EXECUTED" and t["action"] == "BUY")
                h.append({"symbol": s, "quantity": q, "avg_cost": bc / bq if bq else 0, "current_price": self.price(s)})
        hv = sum(x["quantity"] * x["current_price"] for x in h)
        return {"cash_balance": round(self.cash, 2), "holdings": h, "holdings_value": round(hv, 2),
                "total_value": round(self.cash + hv, 2)}

    def guardian_logs(self, limit=50):
        return [{"transaction_id": r["tx"], "decision": r["decision"], "risk_score": r["risk_score"],
                 "rules_triggered": r["rules_triggered"], "reason": r["reason"],
                 "created_at": next(t["reviewed_at"] for t in self.txs if t["transaction_id"] == r["tx"])}
                for r in reversed(self.reviews)][:limit]

    def theory(self):
        return self.theory_obj

    def policy(self):
        return dict(self.policy_obj, trades_last_hour=len(self._executed_recent(3600)),
                    trades_last_day=len(self._executed_recent(86400)))

    def news(self):
        return self.news_items

    def transactions(self, status=None, limit=200):
        out = [{k: v for k, v in t.items() if not k.startswith("_")} for t in reversed(self.txs)]
        return [t for t in out if not status or t["status"] == status]

    def pending(self, limit=20):
        p = [{"transaction_id": t["transaction_id"], "theory_id": t["theory_id"], "theory_version": t["theory_version"],
              "symbol": t["symbol"], "action": t["action"], "requested_quantity": t["requested_quantity"],
              "requested_price": t["requested_price"],
              "estimated_total": round(t["requested_quantity"] * t["requested_price"], 2),
              "reasoning": t["reasoning"], "trader_key_label": t["_label"], "created_at": t["created_at"]}
             for t in self.txs if t["status"] == "PENDING"]
        pol = self.policy()
        return {"pending": p, "context": {"trades_last_hour": pol["trades_last_hour"],
                                          "trades_last_day": pol["trades_last_day"], "policy": self.policy_obj}}

    def propose(self, symbol, action, quantity, reasoning, theory_id, theory_version):
        if symbol not in self.policy_obj["allowed_symbols"]:
            raise RuntimeError("symbol_not_allowed")
        t = self.inject(action, float(quantity), symbol, reasoning=reasoning)
        t["theory_id"], t["theory_version"] = theory_id, theory_version
        if action in self.policy_obj["restricted_actions"]:
            t["status"] = "REJECTED"
        if self.fail_next_propose:
            self.fail_next_propose = False
            from common.ledger_client import UncertainWrite
            raise UncertainWrite("simulated lost proposal response")
        return {"transaction_id": t["transaction_id"], "status": t["status"], "market_price": t["requested_price"]}

    def review(self, transaction_id, decision, reason, risk_score, modified_quantity=None, rules_triggered=None):
        t = next(t for t in self.txs if t["transaction_id"] == transaction_id)
        if t["status"] != "PENDING":
            return {"transaction_id": transaction_id, "status": t["status"], "idempotent": True}
        modified_quantity = float(modified_quantity) if modified_quantity is not None else None
        assert decision in ("APPROVE", "MODIFY", "BLOCK", "ESCALATE") and reason
        self.reviews.append({"tx": transaction_id, "decision": decision, "reason": reason,
                             "risk_score": risk_score, "modified_quantity": modified_quantity,
                             "rules_triggered": rules_triggered or []})
        t.update(guardian_decision=decision, guardian_reason=reason, risk_score=risk_score, reviewed_at=fmt(self.now))
        if decision in ("APPROVE", "MODIFY"):
            q = modified_quantity if decision == "MODIFY" else t["requested_quantity"]
            px = self.price(t["symbol"])
            if t["action"] == "BUY":
                self.cash -= q * px
                self.holdings[t["symbol"]] = self.holdings.get(t["symbol"], 0) + q
            else:
                self.cash += q * px
                self.holdings[t["symbol"]] = self.holdings.get(t["symbol"], 0) - q
            t.update(status="EXECUTED", executed_quantity=q, executed_price=px,
                     executed_total=round(q * px, 2), executed_at=fmt(self.now))
        else:
            t["status"] = {"BLOCK": "BLOCKED", "ESCALATE": "ESCALATED"}[decision]
        if self.fail_next_review:
            self.fail_next_review = False
            from common.ledger_client import UncertainWrite
            raise UncertainWrite("simulated lost review response")
        return {"transaction_id": transaction_id, "status": t["status"]}
