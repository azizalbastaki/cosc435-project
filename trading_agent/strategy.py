"""Pure strategy signals and sizing, shared by live trading and offline replay."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math


@dataclass
class Signal:
    action: str                 # BUY / SELL / HOLD
    quantity: float = 0.0       # units of the symbol
    usd: float = 0.0
    reason: str = ""
    kind: str = ""              # entry / reversion / take_profit / stop_loss / none
    features: dict = field(default_factory=dict)


def avg_cost_from_history(transactions: list[dict], symbol: str) -> tuple[float, float]:
    """(quantity held, average acquisition cost) from executed BUY/SELL history."""
    qty = cost = 0.0
    for t in sorted(transactions, key=lambda t: t.get("transaction_id", 0)):
        if t.get("symbol") != symbol or t.get("status") != "EXECUTED":
            continue
        q = float(t.get("executed_quantity") or 0)
        px = float(t.get("executed_price") or 0)
        if t.get("action") == "BUY" and q > 0:
            qty += q
            cost += q * px
        elif t.get("action") == "SELL" and q > 0 and qty > 0:
            frac = min(q, qty) / qty
            cost -= cost * frac
            qty -= min(q, qty)
    return qty, (cost / qty if qty > 1e-12 else 0.0)


def signal_value(price, samples, theory, *, mean=None, start_price=None):
    """Use the active theory's signal; momentum needs the exact lookback start."""
    mode = theory["strategy"]
    if mode == "momentum":
        reference = start_price if start_price is not None else samples[0]
        key = "lookback_return_pct"
    elif mode == "mean_reversion":
        reference = mean if mean is not None else sum(samples) / len(samples)
        key = "deviation_pct"
    else:
        raise ValueError("unsupported strategy")
    if not all(math.isfinite(float(x)) and float(x) > 0 for x in (price, reference)):
        raise ValueError("signal prices must be finite and positive")
    value = (Decimal(str(price)) / Decimal(str(reference)) - 1) * 100
    features = {"strategy": mode, "lookback_minutes": theory["lookback_minutes"], key: float(value)}
    features["lookback_start_price" if mode == "momentum" else "lookback_mean"] = float(reference)
    return value, features


def direction(price, held_qty, avg_cost, theory, value):
    """Risk exits always precede signal entries/exits; no short selling."""
    price, cost = Decimal(str(price)), Decimal(str(avg_cost))
    if held_qty > 0 and cost > 0:
        if price <= cost * (1 - Decimal(str(theory["stop_loss_pct"])) / 100):
            return "SELL", "stop_loss"
        if price >= cost * (1 + Decimal(str(theory["take_profit_pct"])) / 100):
            return "SELL", "take_profit"
    threshold = Decimal(str(theory["threshold_pct"]))
    if theory["strategy"] == "momentum":
        buy, sell = value >= threshold, value <= -threshold
    elif theory["strategy"] == "mean_reversion":
        buy, sell = value <= -threshold, value >= threshold
    else:
        raise ValueError("unsupported strategy")
    if sell and held_qty > 0:
        return "SELL", "momentum_exit" if theory["strategy"] == "momentum" else "reversion"
    if buy:
        return "BUY", "entry"
    return "HOLD", "none"


def decide(price: float, samples: list[float] | None, held_qty: float, avg_cost: float, cash: float,
           theory: dict, limits: dict, mean: float | None = None, start_price: float | None = None) -> Signal:
    value, feats = signal_value(price, samples, theory, mean=mean, start_price=start_price)
    feats.update(price=round(price, 6), held_qty=round(held_qty, 8),
                 avg_cost=round(avg_cost, 6), cash=round(cash, 2))
    action, kind = direction(price, held_qty, avg_cost, theory, value)
    reason = f"{theory['strategy']} {kind}: signal {value:+.4f}%, threshold {theory['threshold_pct']}%"
    max_usd = float(limits["max_order_usd"])
    if action == "SELL":
        qty = round(min(held_qty, max_usd / price), 8)
        return Signal(action, qty, qty * price, reason, kind, feats)
    if action == "BUY":
        usd = min(cash * float(theory["position_fraction"]), max_usd, cash)
        if usd >= float(limits.get("min_order_usd", 100)):
            qty = round(usd / price, 8)
            return Signal(action, qty, qty * price, reason, kind, feats)
        reason += "; insufficient order budget"
    return Signal("HOLD", reason=reason, kind="none", features=feats)
