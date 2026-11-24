"""Exact money arithmetic (Student Development Guide 6.3).

Ledger settles BUY cash rounded UP to the cent and SELL proceeds rounded DOWN; policy limits must be
compared with the LARGER of the exact notional and the settled amount, so rounding can never hide an
oversized order. Quantities have at most 10 decimals and are rounded DOWN.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

CENT = Decimal("0.01")
QSTEP = Decimal("1e-10")


def D(x) -> Decimal:
    """Decimal from a number or decimal string; rejects bools, NaN and infinity."""
    if isinstance(x, bool):
        raise ValueError("boolean is not a number")
    if isinstance(x, float) and not math.isfinite(x):
        raise ValueError("non-finite number")
    try:
        d = Decimal(str(x))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {x!r}") from exc
    if not d.is_finite():
        raise ValueError("non-finite number")
    return d


def cash_amount(quantity, price, action: str) -> Decimal:
    """Cash the ledger settles, to the cent (BUY rounds up, SELL rounds down)."""
    exact = D(quantity) * D(price)
    return exact.quantize(CENT, rounding=ROUND_CEILING if action == "BUY" else ROUND_FLOOR)


def policy_amount(quantity, price, action: str) -> Decimal:
    """Amount to compare with policy limits: the larger of exact and settled."""
    exact = D(quantity) * D(price)
    return max(exact, cash_amount(quantity, price, action))


def bounded_quantity(budget, price) -> Decimal:
    """Largest quantity a budget buys: budget to cents DOWN, quantity to 10 dp DOWN."""
    cents = D(budget).quantize(CENT, rounding=ROUND_FLOOR)
    if cents <= 0 or D(price) <= 0:
        return Decimal(0)
    return (cents / D(price)).quantize(QSTEP, rounding=ROUND_FLOOR)


def floor_qty(q) -> Decimal:
    return D(q).quantize(QSTEP, rounding=ROUND_FLOOR)


def decimals_ok(x, places: int) -> bool:
    """True if x has at most `places` decimal places."""
    d = D(x)
    return d == d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_FLOOR)


def qstr(q) -> str:
    """Serialise a quantity as a plain decimal string (Ledger accepts decimal strings)."""
    s = format(floor_qty(q), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s
