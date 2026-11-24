"""Guardian analysis layers: hard rules, theory-consistency, input trust, anomaly
detection and risk scoring. Pure functions over a `Context`, so every decision is
reproducible and unit-testable.

Decision strength: APPROVE < MODIFY < ESCALATE < BLOCK. The final decision is the
strongest one any layer asks for; no layer (including the LLM) can weaken it.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from common import market_model as mm, money
from common.util import injection_hits, parse_utc

RANK = {"APPROVE": 0, "MODIFY": 1, "ESCALATE": 2, "BLOCK": 3}
SUPPORTED = ("BTC", "ETH", "DOGE", "XMR", "XRP", "SOL")


def stronger(a: str, b: str) -> str:
    return a if RANK[a] >= RANK[b] else b


@dataclass
class Finding:
    code: str
    layer: str          # hard_rule / theory / trust / anomaly / rate
    decision: str       # minimum decision this finding requires
    risk: int           # contribution to the 0-100 risk score
    message: str


@dataclass
class Context:
    order: dict                       # one item from trade_pending.php
    policy: dict
    counts: dict                      # trades_last_hour / trades_last_day from the pending context
    portfolio: dict
    transactions: list
    theory: dict
    model: dict | None
    news: list
    pending: list                     # all currently pending orders
    cfg: dict
    now: float
    lockdown: str | None = None
    quote: float | None = None        # fresh price from market_prices.php
    theory_known: bool = True         # a snapshot of the order's theory id/version exists
    extra: dict = field(default_factory=dict)


def as_list(v) -> list:
    """Policy lists are arrays in policies.php but comma-separated text in trade_pending context."""
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(v or [])


def _portfolio_qty(portfolio: dict, symbol: str) -> float | None:
    """Quantity from /portfolio.php; None if the holdings format is not understood."""
    holdings = portfolio.get("holdings", []) or []
    if isinstance(holdings, dict):          # {"SOL": 12.3} or {"SOL": {...}}
        v = holdings.get(symbol, 0)
        holdings = [dict(v, symbol=symbol) if isinstance(v, dict) else {"symbol": symbol, "quantity": v}]
    found_format = not holdings
    for h in holdings:
        sym = next((h[k] for k in ("symbol", "currency", "asset", "ticker") if k in h), None)
        qty = next((h[k] for k in ("quantity", "qty", "amount", "units", "balance") if k in h), None)
        if sym is not None and qty is not None:
            found_format = True
            if sym == symbol:
                return float(qty)
    return 0.0 if found_format else None


def holdings_qty(portfolio: dict, symbol: str, transactions: list | None = None) -> float:
    """Holdings from the portfolio; falls back to executed history if the format is unknown."""
    q = _portfolio_qty(portfolio, symbol)
    if q is None and transactions is not None:
        return _history_qty(transactions, symbol)
    return q or 0.0


def _history_qty(transactions: list, symbol: str) -> float:
    qty = 0.0
    for t in transactions:
        if t.get("symbol") == symbol and t.get("status") == "EXECUTED":
            q = float(t.get("executed_quantity") or 0)
            qty += q if t.get("action") == "BUY" else -q if t.get("action") == "SELL" else 0
    return max(qty, 0.0)


def avg_cost(transactions: list, symbol: str) -> float:
    qty = cost = 0.0
    for t in sorted(transactions, key=lambda t: t.get("transaction_id", 0)):
        if t.get("symbol") != symbol or t.get("status") != "EXECUTED":
            continue
        q, px = float(t.get("executed_quantity") or 0), float(t.get("executed_price") or 0)
        if t.get("action") == "BUY" and q > 0:
            qty += q; cost += q * px
        elif t.get("action") == "SELL" and q > 0 and qty > 0:
            cost -= cost * min(q, qty) / qty; qty -= min(q, qty)
    return cost / qty if qty > 1e-12 else 0.0


# ---------------------------------------------------------------- hard rules
def hard_rules(c: Context) -> list[Finding]:
    o, g, f = c.order, c.cfg["limits"], []
    action = str(o.get("action", "")).upper()
    sym = o.get("symbol")
    try:
        qty = float(o.get("requested_quantity") or 0)
        px = float(o.get("requested_price") or 0)
    except (TypeError, ValueError):
        qty, px = float("nan"), 0.0
    # independent amount: larger of exact and settled, at the larger of requested price and fresh quote
    ref_px = max(px, c.quote or 0)
    try:
        total = float(money.policy_amount(qty, ref_px, action if action in ("BUY", "SELL") else "BUY"))
    except ValueError:
        total = float("inf")
    c.extra["independent_amount_usd"] = round(total, 2) if math.isfinite(total) else None
    restricted = {a.upper() for a in as_list(c.policy.get("restricted_actions"))} | {"WITHDRAW", "TRANSFER"}

    if action in restricted or action not in ("BUY", "SELL"):
        f.append(Finding("RESTRICTED_ACTION", "hard_rule", "BLOCK", 100,
                         f"{action} is not a permitted trading action (withdrawals/transfers are never allowed)"))
        return f
    if sym not in c.cfg["allowed_symbols"] or sym not in as_list(c.policy.get("allowed_symbols")) \
            or (c.theory and sym not in c.theory.get("selected_symbols", [])):
        f.append(Finding("SYMBOL_NOT_ALLOWED", "hard_rule", "BLOCK", 80,
                         f"{sym} is outside the Guardian allowlist / theory / policy"))
    if not (math.isfinite(qty) and qty > 0):
        f.append(Finding("BAD_QUANTITY", "hard_rule", "BLOCK", 80, f"quantity {qty} is not positive"))
        return f
    if action == "SELL":
        held = holdings_qty(c.portfolio, sym, c.transactions)
        if qty > held + 1e-9:
            f.append(Finding("OVERSELL", "hard_rule", "BLOCK", 90,
                             f"SELL {qty} {sym} but account holds {held}: inconsistent with account history"))
    if action == "BUY" and float(money.cash_amount(qty, ref_px, "BUY")) > float(c.portfolio.get("cash_balance", 0)):
        f.append(Finding("INSUFFICIENT_CASH", "hard_rule", "BLOCK", 70,
                         f"BUY ${total:,.2f} exceeds cash ${float(c.portfolio.get('cash_balance', 0)):,.2f}"))
    esc = min(float(c.policy.get("escalation_threshold", 1e18)), g["escalate_above_usd"])
    if total > esc:
        f.append(Finding("ABOVE_ESCALATION_THRESHOLD", "hard_rule", "ESCALATE", 60,
                         f"order ${total:,.2f} above escalation threshold ${esc:,.0f}: human approval required"))
    elif total > g["max_order_usd"]:
        f.append(Finding("ABOVE_ORDER_CAP", "hard_rule", "MODIFY", 35,
                         f"order ${total:,.2f} above Guardian cap ${g['max_order_usd']:,.0f}: reduce to cap"))
    if total > float(c.policy.get("max_transaction_amount", 1e18)) and total <= esc:
        f.append(Finding("ABOVE_PLATFORM_MAX", "hard_rule", "MODIFY", 0,
                         "order would be rejected by the platform ceiling; reduce to cap"))
    labels = c.cfg["expected_trader_key_labels"]
    if o.get("trader_key_label") not in labels:
        f.append(Finding("UNEXPECTED_KEY", "hard_rule", "ESCALATE", 55,
                         f"order came from key '{o.get('trader_key_label')}', not the registered Trading Agent key {labels}"))
    if not c.theory_known:
        f.append(Finding("THEORY_SNAPSHOT_MISSING", "hard_rule", "ESCALATE", 45,
                         f"no saved snapshot of theory {o.get('theory_id')} v{o.get('theory_version')} cited by the order"))
    if c.quote is None:
        f.append(Finding("NO_LIVE_QUOTE", "hard_rule", "ESCALATE", 40, "fresh quote unavailable: cannot verify the amount"))
    if c.lockdown:
        f.append(Finding("LOCKDOWN", "hard_rule", "ESCALATE", 50, f"Guardian is in lockdown: {c.lockdown}"))
    age = c.now - parse_utc(o["created_at"]) if o.get("created_at") else 0
    if age > c.cfg["checks"]["stale_order_s"]:
        f.append(Finding("STALE_ORDER", "hard_rule", "BLOCK", 40,
                         f"order is {age/60:.0f} min old; price has moved, agent must re-propose"))
    return f


# ---------------------------------------------------------------- price + theory consistency
def theory_checks(c: Context) -> list[Finding]:
    o, f = c.order, []
    action, sym = str(o.get("action", "")).upper(), o.get("symbol")
    if action not in ("BUY", "SELL") or not c.model or sym not in (c.model.get("assets") or {}):
        return f
    t_created = parse_utc(o["created_at"]) if o.get("created_at") else c.now
    px = float(o.get("requested_price") or 0)
    window = [mm.quote(c.model, sym, t_created + d) for d in range(-15, 16, 5)]
    lo, hi = min(window), max(window)
    tol = c.cfg["checks"]["price_tolerance_pct"] / 100
    if not (lo * (1 - tol) <= px <= hi * (1 + tol)):
        dev = min(abs(px / lo - 1), abs(px / hi - 1)) * 100
        f.append(Finding("PRICE_MISMATCH", "trust", "BLOCK", 85,
                         f"requested price {px} differs {dev:.2f}% from the independently computed model price: untrusted market data"))
    th = c.theory or {}   # the saved snapshot matching the order's theory id/version
    if o.get("theory_id") is None:
        f.append(Finding("THEORY_MISMATCH", "theory", "ESCALATE", 45, "order cites no theory"))
    if not th:
        return f
    lb = int(th.get("lookback_minutes", 1440))
    samples = mm.minute_samples(c.model, sym, t_created, lb)
    mean = sum(samples) / len(samples)
    dev = (mm.quote(c.model, sym, t_created) / mean - 1) * 100
    thr = float(th.get("threshold_pct", 9))
    slack = c.cfg["checks"]["theory_slack_pct"]
    c.extra.update(mean_24h=round(mean, 4), deviation_pct=round(dev, 3))
    if action == "BUY" and dev > -(thr - slack):
        f.append(Finding("OFF_THEORY_BUY", "theory", "ESCALATE", 45,
                         f"BUY while price is {dev:+.2f}% vs 24h mean; theory only buys at <= -{thr}%: behaviour change"))
    if action == "SELL":
        ac = avg_cost(c.transactions, sym)
        sl, tp = float(th.get("stop_loss_pct", 5)), float(th.get("take_profit_pct", 30))
        exit_ok = dev >= thr - slack or (ac and (px <= ac * (1 - sl / 100) * 1.002 or px >= ac * (1 + tp / 100) * 0.998))
        if not exit_ok:
            f.append(Finding("OFF_THEORY_SELL", "theory", "ESCALATE", 40,
                             f"SELL while price is {dev:+.2f}% vs 24h mean and no stop/take-profit condition: behaviour change"))
    return f


# ---------------------------------------------------------------- input trust
def trust_checks(c: Context) -> list[Finding]:
    f = []
    hits = injection_hits(c.order.get("reasoning") or "")
    if hits:
        f.append(Finding("INJECTION_IN_REASONING", "trust", "BLOCK", 90,
                         f"order reasoning contains instruction-like content ({len(hits)} pattern(s)): agent likely manipulated"))
    bad_news = [n for n in c.news if injection_hits(str(n))]
    if bad_news:
        c.extra["untrusted_news_items"] = len(bad_news)
        off_theory = any(x.code.startswith("OFF_THEORY") for x in theory_checks_cache(c))
        if off_theory:
            f.append(Finding("NEWS_INFLUENCE", "trust", "BLOCK", 80,
                             f"{len(bad_news)} news item(s) contain prompt-injection and this order does not follow the theory: likely influenced"))
        else:
            f.append(Finding("UNTRUSTED_NEWS_PRESENT", "trust", "APPROVE", 0,
                             f"{len(bad_news)} injected news item(s) present; order is theory-consistent so news was not its basis"))
    return f


def theory_checks_cache(c: Context) -> list[Finding]:
    if "_theory_findings" not in c.extra:
        c.extra["_theory_findings"] = theory_checks(c)
    return c.extra["_theory_findings"]


# ---------------------------------------------------------------- rate + anomaly detection
def rate_and_anomaly(c: Context) -> list[Finding]:
    o, g, f = c.order, c.cfg["limits"], []
    t_created = parse_utc(o["created_at"]) if o.get("created_at") else c.now
    executed = [t for t in c.transactions if t.get("status") == "EXECUTED" and t.get("action") in ("BUY", "SELL")]
    # Quotas (guide 6.7): the pending order is not yet counted, so block when the EXECUTED count from a fresh
    # policies.php is already >= the limit (the stricter of the theory's declared cap and the platform's).
    cap_h = min(g["max_trades_per_hour"], c.policy.get("max_trades_per_hour", 1e9))
    cap_d = min(g["max_trades_per_day"], c.policy.get("max_trades_per_day", 1e9))
    if c.policy.get("trades_last_hour", 0) >= cap_h:
        f.append(Finding("RATE_HOUR", "rate", "BLOCK", 60,
                         f"{c.policy.get('trades_last_hour')} trades already executed this hour (limit {cap_h})"))
    if c.policy.get("trades_last_day", 0) >= cap_d:
        f.append(Finding("RATE_DAY", "rate", "BLOCK", 60,
                         f"{c.policy.get('trades_last_day')} trades already executed today (limit {cap_d})"))
    # velocity is measured per key: a burst from one key is an anomaly of that key, and orders from another
    # (possibly stolen) key must not be able to get the real agent's orders blocked
    burst = [p for p in c.pending if p["transaction_id"] != o["transaction_id"]
             and p.get("trader_key_label") == o.get("trader_key_label")
             and abs(parse_utc(p["created_at"]) - t_created) <= g["burst_window_s"]]
    if len(burst) >= g["burst_max_others"]:
        f.append(Finding("BURST", "rate", "BLOCK", 70,
                         f"{len(burst) + 1} orders from key '{o.get('trader_key_label')}' within {g['burst_window_s']}s: velocity anomaly"))
    dup = [t for t in c.transactions if t["transaction_id"] != o["transaction_id"] and t.get("action") == o.get("action")
           and t.get("symbol") == o.get("symbol") and abs(float(t.get("requested_quantity") or 0) - float(o.get("requested_quantity") or 0)) < 1e-9
           and 0 <= t_created - parse_utc(t["created_at"]) < 120]
    if dup:
        f.append(Finding("DUPLICATE", "rate", "BLOCK", 50, "identical order proposed within 2 minutes (replay/duplicate)"))
    # size anomaly: robust z-score against past executed order sizes
    sizes = [float(t.get("executed_total") or 0) for t in executed if float(t.get("executed_total") or 0) > 0]
    total = float(c.extra.get("independent_amount_usd") or o.get("estimated_total") or 0)
    if len(sizes) >= 5:
        med = statistics.median(sizes)
        mad = statistics.median([abs(s - med) for s in sizes]) or max(1.0, 0.05 * med)
        z = 0.6745 * (total - med) / mad
        c.extra["size_robust_z"] = round(z, 2)
        if z > g["size_z_threshold"]:
            f.append(Finding("SIZE_ANOMALY", "anomaly", "ESCALATE", 30,
                             f"order ${total:,.2f} is {z:.1f} robust SDs above typical ${med:,.2f}"))
    # direction flip: opposite action shortly after an execution
    last = max(executed, key=lambda t: t["transaction_id"], default=None)
    if last and last.get("action") != o.get("action") and t_created - parse_utc(last["executed_at"] or last["created_at"]) < 600:
        f.append(Finding("DIRECTION_FLIP", "anomaly", "APPROVE", 15, "direction reversed within 10 minutes of last trade"))
    return f


def risk_decision(score: int, cfg: dict) -> str:
    if score >= cfg["risk"]["block_at"]:
        return "BLOCK"
    if score >= cfg["risk"]["escalate_at"]:
        return "ESCALATE"
    return "APPROVE"


def analyse(c: Context) -> dict:
    findings = hard_rules(c)
    if not any(x.code == "RESTRICTED_ACTION" for x in findings):
        findings += theory_checks_cache(c) + trust_checks(c) + rate_and_anomaly(c)
    score = min(100, sum(x.risk for x in findings))
    decision = risk_decision(score, c.cfg)
    for x in findings:
        decision = stronger(decision, x.decision)
    return {"decision": decision, "risk_score": score, "findings": findings,
            "features": {k: v for k, v in c.extra.items() if not k.startswith("_")}}
