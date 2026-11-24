"""Small, credential-free observability helpers; never change trading decisions."""
from __future__ import annotations
import math


def emit(log, event, **fields):
    try:
        log(event, **fields)
    except Exception:
        pass  # observing a call must never retry it or change its outcome


def safe_fields(data, names):
    if not isinstance(data, dict):
        return {}
    return {k: v for k in names if (v := data.get(k)) is not None and
            (isinstance(v, (str, bool, int)) or isinstance(v, float) and math.isfinite(v))}


def response_summary(endpoint, data):
    result = safe_fields(data, ['transaction_id', 'status', 'action', 'symbol', 'quantity', 'theory_version', 'idempotent'])
    for field in ['prices', 'news', 'holdings', 'transactions', 'pending', 'guardian_logs']:
        if isinstance(data.get(field), list):
            result[field + '_count'] = len(data[field])
    if endpoint == 'portfolio.php':
        result.update(safe_fields(data, ['cash_balance', 'holdings_value', 'total_value']))
    if endpoint == 'policies.php':
        result.update(safe_fields(data, ['trades_last_hour', 'trades_last_day', 'max_transaction_amount', 'max_trades_per_hour', 'escalation_threshold']))
    if isinstance(data.get('fill'), dict):
        result['fill'] = safe_fields(data['fill'], ['quantity', 'price', 'total'])
    if isinstance(data.get('theory'), dict):
        result['theory'] = safe_fields(data['theory'], ['id', 'version', 'strategy', 'status'])
    if isinstance(data.get('model'), dict):
        result['model_version'] = data['model'].get('version')
    return result
