"""Model contracts and fixture-only evidence boundary for the offline agents."""

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

from model_adapter import ModelError
from validation import number, normalize_list, ValidationError

TRADER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"symbol": {"type": "string"},
                   "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                   "quantity": {"type": "number"}, "reasoning": {"type": "string"}},
    "required": ["symbol", "action", "quantity", "reasoning"],
}
GUARDIAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "MODIFY", "BLOCK", "ESCALATE"]},
        "reason": {"type": "string"}, "risk_score": {"type": "number"},
        "modified_quantity": {"type": ["number", "null"]},
        "rules_triggered": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "reason", "risk_score", "modified_quantity", "rules_triggered"],
}


def model_result(model, prompt, payload, schema):
    result = model.generate(prompt, payload, schema)
    try:
        Draft202012Validator(schema).validate(result)
    except SchemaError:
        raise ModelError("Model response does not match the agent contract") from None
    return result


def evidence(source, symbol):
    """Read evidence through this role's source, never through the other agent.

    Only implemented by the offline fixture source for now. The strategy object
    is synthetic; this is not a replacement for active theory validation/replay.
    """
    policy = source.policy()
    portfolio = source.portfolio()
    quote = source.quote(symbol)
    strategy = source.strategy()
    # Select fields explicitly instead of forwarding whole source responses.
    data = {
        "policy": {k: policy[k] for k in (
            "allowed_symbols", "restricted_actions", "max_transaction_amount",
            "max_trades_per_hour", "max_trades_per_day", "trades_last_hour",
            "trades_last_day", "escalation_threshold")},
        "portfolio": {"cash_balance": portfolio["cash_balance"],
                      "holdings": [{"symbol": h["symbol"], "quantity": h["quantity"]}
                                   for h in portfolio["holdings"]]},
        "quote": {"symbol": quote["symbol"], "price": quote["price"]},
        "selected_symbols": strategy["selected_symbols"],
        "position_fraction": strategy["position_fraction"],
    }
    try:
        for field in ("max_transaction_amount", "escalation_threshold"):
            number(data["policy"][field])
        for field in ("allowed_symbols", "restricted_actions"):
            normalize_list(data["policy"][field])
        for field in ("max_trades_per_hour", "max_trades_per_day", "trades_last_hour", "trades_last_day"):
            value = data["policy"][field]
            if type(value) is not int or value < 0:
                raise ValueError
        number(data["portfolio"]["cash_balance"])
        if data["quote"]["symbol"] != symbol or number(data["quote"]["price"]) <= 0:
            raise ValueError
        seen = set()
        for holding in data["portfolio"]["holdings"]:
            if not isinstance(holding["symbol"], str) or holding["symbol"] in seen:
                raise ValueError
            seen.add(holding["symbol"])
            number(holding["quantity"])
        normalize_list(data["selected_symbols"])
        fraction = number(data["position_fraction"])
        if not 0 < fraction <= 1:
            raise ValueError
    except (ValidationError, ValueError, TypeError):
        raise ModelError("Malformed independent evidence") from None
    return data
