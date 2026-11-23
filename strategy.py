"""Shared pure strategy calculations. Each agent supplies its own fresh evidence."""

from decimal import Decimal, localcontext

from validation import ValidationError, bounded_quantity, number, validate_order


def decision_bounds(theory, signals, policy, portfolio, symbol):
    """Calculate allowed directions and quantities; the model still decides."""
    if theory.strategy == "custom":
        raise ValidationError("Custom strategy rules need explicit implementation; HOLD")
    price = signals.current_price
    cash = number(portfolio["cash_balance"])
    cap = number(policy["max_transaction_amount"])
    owned = Decimal(0)
    average_cost = Decimal(0)
    for holding in portfolio["holdings"]:
        if holding["symbol"] == symbol:
            owned = number(holding["quantity"])
            average_cost = number(holding["avg_cost"])
            if owned > 0 and average_cost <= 0:
                raise ValidationError("Owned position needs a positive average cost")

    with localcontext() as context:
        context.prec = 80
        budget = min(cash * theory.position_fraction, cap)
        max_buy = bounded_quantity(budget, price)
        max_sell = min(owned, bounded_quantity(cap, price))
        position_return = (price / average_cost - 1) * 100 if owned else None

    signal = signals.strategy_signal(theory.strategy)
    threshold = theory.threshold_pct
    # A tiny tolerance is only for computed signals, never money or policy limits.
    epsilon = Decimal("1e-12")
    if theory.strategy == "momentum":
        buy_signal = signal >= threshold - epsilon
        sell_signal = signal <= -threshold + epsilon
    else:
        buy_signal = signal > threshold + epsilon
        sell_signal = signal < -threshold - epsilon

    risk_exit = position_return is not None and (
        position_return <= -theory.stop_loss_pct or position_return >= theory.take_profit_pct)
    actions = ["HOLD"]
    if risk_exit or (sell_signal and owned > 0):
        if max_sell > 0:
            actions.append("SELL")
    elif buy_signal and max_buy > 0:
        actions.append("BUY")

    # Remove actions that fail the same local gate used on the final model output.
    context = dict(policy=policy, portfolio=portfolio, quote={"symbol": symbol, "price": price},
                   selected_symbols=list(theory.selected_symbols), position_fraction=theory.position_fraction)
    permitted = ["HOLD"]
    for action in actions[1:]:
        quantity = max_buy if action == "BUY" else max_sell
        try:
            validate_order({"symbol": symbol, "action": action, "quantity": quantity,
                            "reasoning": "Pre-model eligibility check."}, **context)
            permitted.append(action)
        except ValidationError:
            pass
    return {"allowed_actions": permitted, "buy_budget": budget,
            "max_buy_quantity": max_buy, "max_sell_quantity": max_sell,
            "position_return_pct": position_return, "risk_exit": risk_exit}

