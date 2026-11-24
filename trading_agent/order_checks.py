"""Local order checks run before anything is sent to Ledger.

The Guardian and the platform policy are the authoritative controls; these checks
exist so that the agent itself never *asks* for something unsafe (defence in depth).
"""
from __future__ import annotations

import math

from common import money

ALLOWED_ACTIONS = {"BUY", "SELL"}


def check_order(action: str, symbol: str, qty: float, price: float, *, cfg: dict, theory: dict,
                policy: dict, cash: float, held_qty: float, recent_trade_times: list[float],
                now: float, open_own_orders: int) -> list[str]:
    """Return a list of violations; empty list = order may be proposed."""
    v = []
    lim = cfg["limits"]
    try:
        amount = float(money.policy_amount(qty, price, action if action in ALLOWED_ACTIONS else "BUY"))
        settled = float(money.cash_amount(qty, price, action if action in ALLOWED_ACTIONS else "BUY"))
    except ValueError:
        return ["quantity/price are not valid numbers"]
    usd = amount
    if action not in ALLOWED_ACTIONS:
        v.append(f"action {action} is never proposed by this agent")
    if action in [a.upper() for a in policy.get("restricted_actions", [])]:
        v.append(f"{action} is restricted by Security Policy")
    if symbol != cfg["symbol"] or symbol not in theory.get("selected_symbols", []) \
            or symbol not in policy.get("allowed_symbols", []):
        v.append(f"{symbol} must be the configured symbol and allowed by both theory and policy")
    if isinstance(qty, bool) or not (math.isfinite(float(qty)) and float(qty) > 0) or not money.decimals_ok(qty, 10):
        v.append("quantity must be positive with at most 10 decimals")
    if settled < 0.01:
        v.append("order settles to less than one cent")
    if usd > lim["max_order_usd"] + 0.01:
        v.append(f"order ${usd:,.2f} exceeds agent cap ${lim['max_order_usd']:,.0f}")
    if usd > float(policy.get("max_transaction_amount", 1e18)):
        v.append("order exceeds platform max_transaction_amount")
    if action == "BUY" and settled > cash:
        v.append(f"insufficient cash (${cash:,.2f}) for ${settled:,.2f}")
    if action == "BUY" and settled > cash * float(theory.get("position_fraction", 1)) + 0.01:
        v.append(f"BUY ${settled:,.2f} exceeds the theory's per-buy cash fraction {theory.get('position_fraction')}")
    if action == "BUY" and usd < lim.get("min_order_usd", 100):
        v.append("order below minimum size")
    if action == "SELL" and money.D(qty) > money.D(held_qty):
        v.append(f"cannot sell {qty} > held {held_qty} (no shorting / overselling)")
    last_hour = sum(1 for t in recent_trade_times if now - t < 3600)
    last_day = sum(1 for t in recent_trade_times if now - t < 86400)
    if last_hour >= lim["max_trades_per_hour"]:
        v.append(f"agent hourly cap reached ({last_hour}/{lim['max_trades_per_hour']})")
    if last_day >= lim["max_trades_per_day"]:
        v.append(f"agent daily cap reached ({last_day}/{lim['max_trades_per_day']})")
    # account-wide executed trades (server count) also count against the theory's caps, so a trade made by
    # anyone else (e.g. a manual test) can never push the agent over the limit the Guardian enforces
    if policy.get("trades_last_hour", 0) >= lim["max_trades_per_hour"]:
        v.append(f"account already made {policy.get('trades_last_hour')} trades this hour (theory cap {lim['max_trades_per_hour']})")
    if policy.get("trades_last_day", 0) >= lim["max_trades_per_day"]:
        v.append(f"account already made {policy.get('trades_last_day')} trades today (theory cap {lim['max_trades_per_day']})")
    if policy.get("trades_last_hour", 0) >= policy.get("max_trades_per_hour", 1e9):
        v.append("platform hourly limit reached")
    if policy.get("trades_last_day", 0) >= policy.get("max_trades_per_day", 1e9):
        v.append("platform daily limit reached")
    if open_own_orders > 0:
        v.append(f"{open_own_orders} own order(s) still pending/escalated; wait for Guardian/human")
    return v
