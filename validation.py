"""Offline trader order checks, based on guide Sections 6.3–6.5.

No network calls or trade submission. Callers must separately validate the active
theory and obtain fresh evidence before using these checks in a trading cycle.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

SYMBOLS = frozenset({"BTC", "ETH", "DOGE", "XMR", "XRP", "SOL"})
CENT = Decimal("0.01")


class ValidationError(ValueError):
    """A local rejection with an optional stable, safe rule name."""

    def __init__(self, message, rule="invalid_input"):
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class ValidatedOrder:
    symbol: str
    action: str
    quantity: Decimal
    reasoning: str
    exact_amount: Decimal
    settled_amount: Decimal


def number(value):
    """Accept JSON numbers or internal Decimal values, never bool/string/NaN."""
    if type(value) not in (int, float, Decimal):
        raise ValidationError("Expected a numeric value")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result >= Decimal("1e18"):
        raise ValidationError("Number must be finite, nonnegative, and below 10^18")
    if result.as_tuple().exponent < -10:
        raise ValidationError("Number exceeds ten decimal places")
    return result


def normalize_list(value):
    """Support policies.php arrays and pending-context comma-separated lists."""
    if isinstance(value, str):
        value = value.split(",") if value.strip() else []
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValidationError("Invalid policy or selected-symbol list")
    return {x.strip().upper() for x in value}


def cash_amount(quantity, price, action):
    if action not in ("BUY", "SELL"):
        raise ValidationError("Settlement requires BUY or SELL")
    with localcontext() as context:
        context.prec = 80
        exact = number(quantity) * number(price)
        mode = ROUND_CEILING if action == "BUY" else ROUND_FLOOR
        return exact.quantize(CENT, rounding=mode)


def policy_amount(quantity, price, action):
    with localcontext() as context:
        context.prec = 80
        return max(number(quantity) * number(price), cash_amount(quantity, price, action))


def bounded_quantity(budget, price):
    with localcontext() as context:
        context.prec = 80
        budget, price = number(budget), number(price)
        if price <= 0:
            raise ValidationError("Price must be positive")
        return (budget.quantize(CENT, rounding=ROUND_FLOOR) / price).quantize(
            Decimal("1e-10"), rounding=ROUND_FLOOR)


def validate_order(order, *, policy, portfolio, quote, selected_symbols, position_fraction):
    """Validate one candidate against supplied snapshots, returning Decimal values.

    order has exactly symbol/action/quantity/reasoning. quote is one Appendix B
    market_prices entry. selected_symbols and position_fraction must come from a
    separately validated theory in a future live cycle. This result is NOT a
    submission payload or Guardian approval. Invalid input raises ValidationError.
    """
    try:
        with localcontext() as context:
            context.prec = 80
            return _validate(order, policy, portfolio, quote, selected_symbols, position_fraction)
    except (KeyError, TypeError, UnicodeError):
        raise ValidationError("Missing or malformed order/context fields") from None


def _validate(order, policy, portfolio, quote, selected_symbols, position_fraction):
    # 1. Check the order's fields and explanation.
    if not isinstance(order, dict) or set(order) != {"symbol", "action", "quantity", "reasoning"}:
        raise ValidationError("Order must contain exactly symbol, action, quantity, reasoning")
    symbol = order["symbol"]
    action = order["action"]
    reason = order["reasoning"]
    if not isinstance(symbol, str) or action not in ("BUY", "SELL", "HOLD"):
        raise ValidationError("Invalid symbol type or action")
    if not isinstance(reason, str) or not reason.strip() or "\x00" in reason:
        raise ValidationError("A nonempty explanation without NUL characters is required")
    reason = reason.strip()
    if len(reason.encode("utf-8")) > 16000:
        raise ValidationError("Explanation exceeds 16000 UTF-8 bytes")
    quantity = number(order["quantity"])

    # 2. HOLD stops here: it does not need prices, balances, or policy checks.
    if action == "HOLD":
        if quantity != 0:
            raise ValidationError("HOLD quantity must be zero")
        return ValidatedOrder(symbol, action, quantity, reason, Decimal(0), Decimal(0))
    if quantity <= 0:
        raise ValidationError("BUY/SELL quantity must be positive")

    # 3. Check currency/action permissions and executed trade counts.
    check_permissions(symbol, action, policy, selected_symbols)
    check_trade_quotas(policy)

    # 4. Calculate the exact value and the amount Ledger would settle.
    if quote["symbol"] != symbol:
        raise ValidationError("Quote does not match the order currency", "quote_currency")
    price = number(quote["price"])
    if price <= 0:
        raise ValidationError("Price must be positive")
    exact = quantity * price
    settled = cash_amount(quantity, price, action)
    if settled < CENT:
        raise ValidationError("Order must settle to at least one cent", "minimum_amount")
    cap = number(policy["max_transaction_amount"])
    if max(exact, settled) > cap:
        raise ValidationError("Order exceeds the policy transaction ceiling", "transaction_ceiling")

    # 5. A BUY needs cash; a SELL needs owned currency.
    if action == "BUY":
        check_buy_balance(settled, portfolio, position_fraction)
    else:
        check_sell_balance(symbol, quantity, portfolio)

    return ValidatedOrder(symbol, action, quantity, reason, exact, settled)


def check_permissions(symbol, action, policy, selected_symbols):
    selected = normalize_list(selected_symbols)
    if not selected or not selected <= SYMBOLS:
        raise ValidationError("Invalid selected currencies")
    allowed = normalize_list(policy["allowed_symbols"])
    if symbol not in SYMBOLS or symbol not in selected or symbol not in allowed:
        raise ValidationError("Currency must be supported and selected by both strategy and policy", "currency_permission")
    if action in normalize_list(policy["restricted_actions"]):
        raise ValidationError("Action is restricted by policy", "restricted_action")


def check_trade_quotas(policy):
    quota_fields = [
        ("trades_last_hour", "max_trades_per_hour"),
        ("trades_last_day", "max_trades_per_day"),
    ]
    for count_name, limit_name in quota_fields:
        count = policy[count_name]
        limit = policy[limit_name]
        if type(count) is not int or type(limit) is not int or min(count, limit) < 0:
            raise ValidationError("Trade counts and limits must be nonnegative integers")
        if count >= limit:
            raise ValidationError("Executed trade quota is exhausted", limit_name)


def check_buy_balance(cost, portfolio, position_fraction):
    cash = number(portfolio["cash_balance"])
    fraction = number(position_fraction)
    if not Decimal("0.001") <= fraction <= 1 or fraction.as_tuple().exponent < -4:
        raise ValidationError("Position fraction must be 0.001 to 1 with at most four decimals")
    if cost > cash:
        raise ValidationError("Insufficient cash", "insufficient_cash")
    if cost > cash * fraction:
        raise ValidationError("Order exceeds the per-buy cash fraction", "position_fraction")


def check_sell_balance(symbol, quantity, portfolio):
    holdings = portfolio["holdings"]
    if not isinstance(holdings, list) or any(not isinstance(h, dict) for h in holdings):
        raise ValidationError("Malformed holdings")

    owned = Decimal(0)
    found = False
    for holding in holdings:
        if holding["symbol"] != symbol:
            continue
        if found:
            raise ValidationError("Duplicate holdings for currency")
        owned = number(holding["quantity"])
        found = True

    if quantity > owned:
        raise ValidationError("Insufficient currency holdings", "insufficient_holdings")
