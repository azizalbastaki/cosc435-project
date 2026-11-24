"""The submitted SOL theory as code: 24h mean reversion on the power-law + cycle model."""
from __future__ import annotations

from dataclasses import dataclass, field


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


def decide(price: float, samples: list[float] | None, held_qty: float, avg_cost: float, cash: float,
           theory: dict, limits: dict, mean: float | None = None) -> Signal:
    """Pure function: same inputs -> same decision (used live and in replay).

    `mean` may be passed precomputed (rolling sum in replay); otherwise it is the
    average of the 1-minute `samples` over the lookback window."""
    if mean is None:
        mean = sum(samples) / len(samples)
    thr = float(theory["threshold_pct"]) / 100
    dev = price / mean - 1
    feats = {"price": round(price, 6), "mean_24h": round(mean, 6), "deviation_pct": round(dev * 100, 3),
             "held_qty": round(held_qty, 8), "avg_cost": round(avg_cost, 6), "cash": round(cash, 2)}
    max_usd = float(limits["max_order_usd"])

    if held_qty > 0 and avg_cost > 0:
        sell_usd = min(held_qty * price, max_usd)
        sell_qty = round(min(held_qty, sell_usd / price), 8)
        if price <= avg_cost * (1 - float(theory["stop_loss_pct"]) / 100):
            return Signal("SELL", sell_qty, sell_qty * price, "stop loss: price {:.2f} <= {:.0%} below avg cost {:.2f}"
                          .format(price, theory["stop_loss_pct"] / 100, avg_cost), "stop_loss", feats)
        if price >= avg_cost * (1 + float(theory["take_profit_pct"]) / 100):
            return Signal("SELL", sell_qty, sell_qty * price, "take profit: price {:.2f} >= avg cost {:.2f} +{}%"
                          .format(price, avg_cost, theory["take_profit_pct"]), "take_profit", feats)
        if dev >= thr:
            return Signal("SELL", sell_qty, sell_qty * price, "reversion complete: price {:+.2f}% vs 24h mean {:.2f}"
                          .format(dev * 100, mean), "reversion", feats)

    if dev <= -thr:
        usd = min(cash * float(theory["position_fraction"]), max_usd, cash)
        if usd >= float(limits.get("min_order_usd", 100)):
            qty = round(usd / price, 8)
            return Signal("BUY", qty, qty * price, "entry: price {:+.2f}% vs 24h mean {:.2f} (threshold -{}%)"
                          .format(dev * 100, mean, theory["threshold_pct"]), "entry", feats)
    return Signal("HOLD", reason="price {:+.2f}% vs 24h mean, inside +/-{}% band".format(dev * 100, theory["threshold_pct"]),
                  kind="none", features=feats)
