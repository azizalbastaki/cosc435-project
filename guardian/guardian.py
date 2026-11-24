"""AI Guardian for Ledger (COSC 435) - version 7.

Independent security reviewer with its own guardian-scoped key. For each pending order (two at a time,
oldest first, each isolated so one error cannot stop the next):

  1. gather its own evidence: fresh policy + executed counts, portfolio, history, live quote
     (market_prices.php), the saved snapshot of the order's theory id/version, news
  2. hard rules (deterministic; cannot be overridden): restricted action, symbol, quantity, cash/holdings,
     ceiling (larger of exact and settled amount), escalation threshold, quotas (>= executed), key,
     stale order, independent price check, theory consistency, injection, burst/duplicate/size anomalies
  3. LANGUAGE MODEL judgment (required) unless the rules already BLOCKED; strict schema validation;
     model failure or invalid answer -> ESCALATE; final = stricter of rules and model
  4. final recheck before APPROVE/MODIFY: re-read pending, policy and quote; recompute the amount with the
     quantity that will actually execute; rerun the hard rules
  5. one review; read the returned status. Lost response -> reconcile next pass (Ledger is idempotent).
     Ledger cooldown -> leave the order pending.

Usage:
  python -m guardian.guardian --config guardian/config.json --minutes 10   # one <=10-min session
  python -m guardian.guardian --config ... --once [--dry-run]             # one review sweep
  python -m guardian.guardian --config ... --clear-lockdown               # human ends lockdown
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import market_model as mm, money                          # noqa: E402
from common.ledger_client import (Cooldown, LedgerClient, LedgerError,  # noqa: E402
                                  UncertainWrite, startup_check)
from common.llm import ModelAdapter, ModelFailure                     # noqa: E402
from common.util import AuditLog, load_config, load_private_env      # noqa: E402
from guardian import judgment, rules                                  # noqa: E402


class GuardianState:
    def __init__(self, path: Path):
        self.path = path
        self.d = {"reviewed": {}, "recent_blocks": [], "lockdown": None, "escalations": {},
                  "theory_snapshots": {}, "uncertain_reviews": {}}
        if path.exists():
            self.d.update(json.loads(path.read_text()))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, default=str))
        tmp.replace(self.path)


class Guardian:
    def __init__(self, cfg: dict, client: LedgerClient | None = None, clock=time.time, log=None):
        self.cfg = cfg
        self.log = log or AuditLog(cfg.get("log_file", "logs/guardian.jsonl"))
        self.client = client or LedgerClient(cfg["base_url"], cfg["api_key"], log=self.log)
        self.clock = clock
        self.state = GuardianState(Path(cfg.get("state_file", "state/guardian_state.json")))
        self.llm = ModelAdapter(cfg.get("model_adapter", {}), log=self.log)
        self._model_cache = None

    # ------------------------------------------------------------------ human commands
    @property
    def clear_flag(self) -> Path:
        return self.state.path.with_name("clear_lockdown.flag")

    def _human_commands(self):
        if self.clear_flag.exists():
            self.log("lockdown_cleared_by_human", previous=self.state.d["lockdown"])
            self.state.d.update(lockdown=None, recent_blocks=[])
            self.clear_flag.unlink()
            self.state.save()

    # ------------------------------------------------------------------ one sweep
    def run_once(self, dry_run: bool = False) -> list[dict]:
        self.log("flow_stage", stage="queue", detail="Poll pending orders; each review gathers independent evidence.")
        self._human_commands()
        pend = self.client.pending(limit=self.cfg.get("batch_size", 2))
        orders = sorted(pend.get("pending", []), key=lambda p: p["transaction_id"])
        self._follow_escalations()
        if not orders:
            self.log("heartbeat", pending=0, lockdown=self.state.d["lockdown"])
            self.state.save()
            return []
        results = []
        for o in orders:
            tid = str(o["transaction_id"])
            if tid in self.state.d["reviewed"]:
                continue  # already decided before a restart; Ledger would answer idempotently anyway
            try:
                res = self.review_order(o, pend, dry_run)
            except Cooldown as exc:
                self.log("cooldown", wait_s=round(exc.seconds, 1), tx=o["transaction_id"])
                break  # leave the order pending while Ledger is unavailable
            except Exception as exc:  # fail closed, per order
                self.log("guardian_error", tx=o["transaction_id"], error=repr(exc)[:300])
                res = self._submit(o, "ESCALATE", 100, f"Guardian could not complete its evidence/analysis "
                                   f"({type(exc).__name__}); escalating to a human (fail-closed).", None, dry_run,
                                   ["GUARDIAN_INTERNAL_ERROR"])
            if res is not None:
                results.append(res)
        self.state.save()
        return results

    # ------------------------------------------------------------------ evidence
    def _theory_for(self, o: dict) -> tuple[dict, bool]:
        snaps = self.state.d["theory_snapshots"]
        try:
            cur = self.client.theory()
            if cur and cur.get("status") == "SUBMITTED":
                snaps.setdefault(f"{cur['id']}:{cur['version']}", cur)
        except LedgerError as exc:
            self.log("context_warning", what="theory", error=str(exc))
        key = f"{o.get('theory_id')}:{o.get('theory_version')}"
        if key in snaps:
            return snaps[key], True
        return {}, o.get("theory_id") is None  # no theory cited is a separate finding

    def _descriptor(self):
        try:
            m = self.client.market_model()
            mm.validate(m)
            fp = mm.fingerprint(m, self.cfg["allowed_symbols"][0])
            pinned = self.cfg.get("pinned_model_fingerprint")
            if pinned and fp != pinned:
                self._enter_lockdown("market model parameters changed (fingerprint mismatch)")
            self._model_cache = m
        except Exception as exc:
            self.log("context_warning", what="market_model", error=str(exc)[:200])
        return self._model_cache

    def _quote(self, sym: str) -> float | None:
        if sym not in rules.SUPPORTED:
            return None
        q = self.client.quotes([sym]).get(sym)
        return float(q["price"]) if q else None

    # ------------------------------------------------------------------ one order
    def review_order(self, o: dict, pend: dict, dry_run: bool) -> dict | None:
        self.log("flow_stage", stage="evidence", tx=o["transaction_id"], detail="Independently read policy, balances, history, theory, news, and quote.")
        policy, portfolio = self.client.policy(), self.client.portfolio()
        txs = self.client.transactions(limit=200)
        theory, known = self._theory_for(o)
        model = self._descriptor()
        quote = self._quote(o.get("symbol"))
        try:
            news = self.client.news()
        except LedgerError:
            news = []
        c = rules.Context(order=o, policy=policy, counts=pend.get("context", {}), portfolio=portfolio,
                          transactions=txs, theory=theory, model=model, news=news, pending=pend.get("pending", []),
                          cfg=self.cfg, now=self.clock(), lockdown=self.state.d["lockdown"], quote=quote,
                          theory_known=known)
        a = rules.analyse(c)
        rule_decision, rule_risk, findings = a["decision"], a["risk_score"], a["findings"]
        self.log("rule_evaluation", tx=o["transaction_id"], decision=rule_decision, risk_score=rule_risk,
                 findings=[{"code": f.code, "reason": f.message} for f in findings], features=a["features"])
        decision, risk = rule_decision, float(rule_risk)
        codes = [f.code for f in findings]
        mod_qty = None
        model_note = "Model judgment: not requested (hard rules already BLOCK)."
        model_codes: list[str] = []

        # 3. model judgment (required unless the rules already blocked)
        if rule_decision != "BLOCK":
            payload = judgment.build_payload(o, quote, policy, portfolio, a["features"], findings, rule_decision,
                                             rule_risk, theory, news, self.cfg["limits"])
            try:
                out = judgment.validate(self.llm.ask_json(judgment.SYSTEM_PROMPT, payload, judgment.GUARDIAN_SCHEMA,
                                                          "guardian_review"), o["requested_quantity"])
                model_codes = [f"MODEL:{x}" for x in out["rules_triggered"]]
                model_note = (f"Model judgment ({self.llm.label}, {self.llm.last_latency_s}s): {out['decision']}"
                              f" (risk {out['risk_score']}) - {out['reason']}")
                if rules.RANK[out["decision"]] > rules.RANK[decision]:
                    decision = out["decision"]
                risk = max(risk, out["risk_score"])
                if out["decision"] == "MODIFY":
                    mod_qty = out["modified_quantity"]
                self.log("model_judgment", tx=o["transaction_id"], rules=rule_decision, model=out["decision"],
                         final=decision, latency_s=self.llm.last_latency_s, reasoning=out["reason"], risk_score=out["risk_score"])
            except (ModelFailure, judgment.Invalid) as exc:
                kind = "MODEL_UNAVAILABLE" if isinstance(exc, ModelFailure) else "MODEL_OUTPUT_INVALID"
                self.log("model_output_rejected" if kind == "MODEL_OUTPUT_INVALID" else "model_unavailable",
                         tx=o["transaction_id"], error=str(exc)[:200])
                model_note = f"Model judgment ({self.llm.label}): {kind} - {str(exc)[:160]}. Escalating (fail-closed)."
                model_codes = [kind]
                decision = rules.stronger(decision, "ESCALATE")
                risk = max(risk, 50.0)

        # MODIFY quantity: never above the rules' cap, always a decrease
        if decision == "MODIFY":
            cap_q = self._capped_qty(max(float(o.get("requested_price") or 0), quote or 0), policy)
            cands = [q for q in (cap_q if "ABOVE_ORDER_CAP" in codes or "ABOVE_PLATFORM_MAX" in codes else None, mod_qty) if q]
            mod_qty = min(cands) if cands else None
            if not mod_qty or mod_qty >= money.D(o["requested_quantity"]):
                decision, mod_qty = "BLOCK", None
                codes.append("MODIFY_NOT_A_DECREASE")

        # 4. final recheck
        notes = []
        if decision in ("APPROVE", "MODIFY"):
            decision, mod_qty, notes = self._final_recheck(o, decision, mod_qty)
        reason = self._reason(decision, risk, findings, a["features"], model_note, notes, mod_qty)
        res = self._submit(o, decision, round(min(100.0, risk), 2), reason, mod_qty, dry_run, codes + model_codes,
                           llm_only=rules.RANK[decision] > rules.RANK[rule_decision])
        if res is not None:
            res["findings"] = codes + model_codes
        return res

    def _capped_qty(self, px: float, policy: dict):
        if px <= 0:
            return None
        cap = min(money.D(self.cfg["limits"]["max_order_usd"]), money.D(policy.get("max_transaction_amount", 1e18)))
        q = money.bounded_quantity(cap * money.D("0.995"), px)  # small headroom for the fill price
        return q if q > 0 else None

    def _final_recheck(self, o: dict, decision: str, mod_qty):
        """Fresh policy + quote; amount with the quantity that will execute; rerun hard rules."""
        self.log("flow_stage", stage="recheck", tx=o["transaction_id"], detail="Refresh policy, quote and balances before submitting an approval or reduced order.")
        current = {p["transaction_id"]: p for p in self.client.pending(limit=50).get("pending", [])}
        p = current.get(o["transaction_id"])
        if not p:
            return "BLOCK", None, ["recheck: order no longer pending"]
        if any(p.get(k) != o.get(k) for k in ("action", "symbol", "requested_quantity", "requested_price")):
            return "BLOCK", None, ["recheck: order changed between analysis and recheck"]
        pol, port = self.client.policy(), self.client.portfolio()
        quote = self._quote(o["symbol"])
        if quote is None:
            return "ESCALATE", None, ["recheck: fresh quote unavailable"]
        qty = mod_qty if decision == "MODIFY" else money.D(o["requested_quantity"])
        amount = money.policy_amount(qty, quote, o["action"])
        cap = min(money.D(self.cfg["limits"]["max_order_usd"]), money.D(pol.get("max_transaction_amount", 1e18)))
        esc = min(money.D(pol.get("escalation_threshold", 1e18)), money.D(self.cfg["limits"]["escalate_above_usd"]))
        if amount > cap:
            return "BLOCK", None, [f"recheck: ${amount:,.2f} at fresh quote {quote} exceeds ceiling ${cap:,.2f}"]
        if amount > esc:
            return "ESCALATE", None, [f"recheck: ${amount:,.2f} above escalation threshold"]
        if o["action"] == "BUY" and money.cash_amount(qty, quote, "BUY") > money.D(port.get("cash_balance", 0)):
            return "BLOCK", None, ["recheck: cash no longer sufficient"]
        if o["action"] == "SELL" and money.D(qty) > money.D(rules.holdings_qty(port, o["symbol"])):
            return "BLOCK", None, ["recheck: holdings no longer sufficient"]
        lim = self.cfg["limits"]
        if pol.get("trades_last_hour", 0) >= min(lim["max_trades_per_hour"], pol.get("max_trades_per_hour", 1e9)) or \
                pol.get("trades_last_day", 0) >= min(lim["max_trades_per_day"], pol.get("max_trades_per_day", 1e9)):
            return "BLOCK", None, ["recheck: trade quota reached"]
        if o["action"] in [x.upper() for x in rules.as_list(pol.get("restricted_actions"))] or \
                o["symbol"] not in rules.as_list(pol.get("allowed_symbols")):
            return "BLOCK", None, ["recheck: action or symbol no longer allowed by policy"]
        return decision, mod_qty, [f"Final recheck passed at fresh quote {quote}: ${amount:,.2f} within limits."]

    # ------------------------------------------------------------------ 5. review + recovery
    def _submit(self, o, decision, score, reason, mod_qty, dry_run, codes=None, llm_only=False) -> dict | None:
        tid = o["transaction_id"]
        reason = reason.encode()[:7900].decode(errors="ignore")
        rec = {"tx": tid, "decision": decision, "risk_score": score,
               "modified_quantity": money.qstr(mod_qty) if mod_qty else None,
               "action": o.get("action"), "symbol": o.get("symbol"), "total": o.get("estimated_total")}
        if dry_run:
            self.log("dry_run_review", **rec, reason=reason)
            return rec
        try:
            resp = self.client.review(tid, decision, reason, score, rec["modified_quantity"], codes or [])
            rec["server_status"] = resp.get("status")
            if resp.get("idempotent"):
                rec["note"] = "already finalised (idempotent)"
        except UncertainWrite as exc:
            # the review may or may not have been recorded: reconcile on the next sweep (re-reading pending/history);
            # Ledger never executes an order twice, so a later resubmission is safe
            self.state.d["uncertain_reviews"][str(tid)] = {"ts": self.clock(), "decision": decision}
            self.log("review_uncertain", tx=tid, error=str(exc))
            return {**rec, "error": "uncertain"}
        except Cooldown:
            raise
        except LedgerError as exc:
            self.log("review_failed", tx=tid, error=str(exc), payload=exc.payload)
            return {**rec, "error": str(exc)}
        self.state.d["uncertain_reviews"].pop(str(tid), None)
        self.state.d["reviewed"][str(tid)] = {"decision": decision, "ts": self.clock(), "status": rec.get("server_status")}
        if decision == "BLOCK" and o.get("trader_key_label") in self.cfg["expected_trader_key_labels"] and not llm_only:
            self._note_block()
        if decision == "ESCALATE" or rec.get("server_status") == "ESCALATED":
            self.state.d["escalations"][str(tid)] = {"since": self.clock(), "resolved": None}
        self.log("review", **rec, reason=reason)
        return rec

    def _note_block(self):
        now = self.clock()
        b = [t for t in self.state.d["recent_blocks"] if now - t < self.cfg["lockdown"]["window_s"]] + [now]
        self.state.d["recent_blocks"] = b
        if len(b) >= self.cfg["lockdown"]["blocks_to_trigger"] and not self.state.d["lockdown"]:
            self._enter_lockdown(f"{len(b)} orders blocked within {self.cfg['lockdown']['window_s']//60} min:"
                                 " Trading Agent may be compromised")

    def _enter_lockdown(self, why: str):
        if not self.state.d["lockdown"]:
            self.state.d["lockdown"] = why
            self.log("ALERT", message=f"LOCKDOWN: {why}. Every order will be escalated until `--clear-lockdown`.")

    def _follow_escalations(self):
        open_ = {k: v for k, v in self.state.d["escalations"].items() if not v.get("resolved")}
        unsure = dict(self.state.d["uncertain_reviews"])
        if not open_ and not unsure:
            return
        try:
            txs = {str(t["transaction_id"]): t for t in self.client.transactions(limit=200)}
        except LedgerError:
            return
        for k, v in unsure.items():  # lost review responses: was it recorded?
            t = txs.get(k)
            if t and t["status"] != "PENDING":
                self.state.d["reviewed"][k] = {"decision": t.get("guardian_decision"), "ts": self.clock(), "status": t["status"]}
                self.state.d["uncertain_reviews"].pop(k, None)
                self.log("review_reconciled", tx=int(k), status=t["status"])
        for k, v in open_.items():
            t = txs.get(k)
            if t and t["status"] not in ("ESCALATED", "PENDING"):
                v["resolved"] = t["status"]
                self.log("escalation_resolved", tx=int(k), outcome=t["status"],
                         waited_min=round((self.clock() - v["since"]) / 60, 1))

    @staticmethod
    def _reason(decision, risk, findings, features, model_note, notes, mod_qty) -> str:
        head = {"APPROVE": "Approved", "MODIFY": "Modified", "ESCALATE": "Escalated to human", "BLOCK": "Blocked"}[decision]
        parts = [f"{head}. Risk {round(min(100.0, risk), 2)}/100."]
        if mod_qty:
            parts.append(f"Quantity reduced to {money.qstr(mod_qty)}.")
        parts.append("Rule findings: " + (" | ".join(f"[{f.code}] {f.message}" for f in findings) if findings
                                          else "none - order matches the declared theory and limits."))
        if features:
            parts.append("Evidence: " + ", ".join(f"{k}={v}" for k, v in features.items()))
        parts.append(model_note)
        parts.extend(notes)
        return " ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "guardian" / "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--minutes", type=float, default=10, help="session length (course rule: at most 10)")
    ap.add_argument("--unlimited", action="store_true",
                    help="run until stopped (BREAKS the 10-minute course rule; logged as such)")
    ap.add_argument("--clear-lockdown", action="store_true", help="human acknowledgement: leave lockdown")
    a = ap.parse_args(argv)
    load_private_env(ROOT, role="guardian")
    cfg = load_config(a.config, "LEDGER_GUARDIAN_KEY")
    g = Guardian(cfg)
    if a.clear_lockdown:
        g.clear_flag.parent.mkdir(parents=True, exist_ok=True)
        g.clear_flag.write_text("cleared by human\n")
        g._human_commands()
        g.clear_flag.write_text("cleared by human\n")
        print("Lockdown clear requested; a running Guardian applies it at its next sweep.")
        return
    problem = startup_check(g.client, "guardian")
    if problem:
        sys.exit(problem)
    if not g.llm.enabled:
        g.log("ALERT", message="no language model configured: every order will be ESCALATED (course rule)")
    minutes = "unlimited" if a.unlimited else min(a.minutes, 10)
    end = float("inf") if a.unlimited else time.time() + minutes * 60
    if a.unlimited:
        g.log("ALERT", message="UNLIMITED session: no time limit (exceeds the 10-minute course rule)")
    g.log("session_start", minutes=minutes, model=g.llm.label)
    interval = max(15, cfg.get("poll_seconds", 15))
    while True:
        try:
            g.run_once(dry_run=a.dry_run)
        except Cooldown as exc:
            g.log("cooldown", wait_s=round(exc.seconds, 1))
            time.sleep(min(exc.seconds, max(0, end - time.time())))
        except LedgerError as exc:
            g.log("api_error", error=str(exc), payload=getattr(exc, "payload", None))
        except Exception as exc:
            g.log("sweep_error", error=repr(exc)[:300])
        if a.once or time.time() + interval > end:
            break
        g.log("flow_stage", stage="waiting", detail="Review sweep complete. Waiting before the next queue check.", wait_s=interval)
        time.sleep(interval + random.uniform(0, 3))
    g.log("session_end", model_calls=g.llm.calls, model_failures=g.llm.failures)


if __name__ == "__main__":
    main()
