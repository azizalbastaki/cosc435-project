"""Validate theory.php data using guide Sections 5.1/6.3 and Appendix B."""

from dataclasses import dataclass, field
from decimal import Decimal

from validation import SYMBOLS, ValidationError, number


@dataclass(frozen=True)
class Theory:
    id: int
    version: int
    selected_symbols: tuple[str, ...]
    strategy: str
    lookback_minutes: int
    threshold_pct: Decimal
    position_fraction: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    # Untrusted text is retained as data, never instructions or console output.
    title: str = field(repr=False)
    hypothesis: str = field(repr=False)
    entry_rule: str = field(repr=False)
    exit_rule: str = field(repr=False)
    risk_rule: str = field(repr=False)


def positive_integer(value, name):
    if type(value) is not int or value <= 0:
        raise ValidationError(f"Theory {name} must be a positive integer")
    return value


def text_field(value, name, byte_limit):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValidationError(f"Theory {name} must contain nonempty text without NUL")
    if len(value.encode("utf-8")) > byte_limit:
        raise ValidationError(f"Theory {name} exceeds its UTF-8 size limit")
    return value


def setting(value, name, minimum, maximum):
    value = number(value)
    if not Decimal(minimum) <= value <= Decimal(maximum):
        raise ValidationError(f"Theory {name} is outside its allowed range")
    if value.as_tuple().exponent < -4:
        raise ValidationError(f"Theory {name} exceeds four decimal places")
    return value


def validate_theory(reply):
    """Return a validated, immutable theory or raise ValidationError (local HOLD).

    Valid structure does not prove profitability, safe prose, or order compliance.
    Unknown server metadata is ignored; all decision fields below are required.
    """
    try:
        if not isinstance(reply, dict) or "theory" not in reply:
            raise ValidationError("Expected a theory response object")
        data = reply["theory"]
        if data is None:
            raise ValidationError("No active submitted theory")
        if not isinstance(data, dict) or data.get("status") != "SUBMITTED":
            raise ValidationError("Theory must have SUBMITTED status")

        # 1. Identity and history window.
        theory_id = positive_integer(data["id"], "id")
        version = positive_integer(data["version"], "version")
        lookback = positive_integer(data["lookback_minutes"], "lookback_minutes")
        if lookback > 1440:
            raise ValidationError("Theory lookback exceeds 1440 minutes")

        # 2. Supported currencies and strategy.
        symbols = data["selected_symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 6:
            raise ValidationError("Theory must select one to six currencies")
        if any(not isinstance(symbol, str) or symbol not in SYMBOLS for symbol in symbols):
            raise ValidationError("Theory contains an unsupported currency")
        if len(set(symbols)) != len(symbols):
            raise ValidationError("Theory contains duplicate currencies")
        strategy = data["strategy"]
        if strategy not in ("momentum", "mean_reversion", "custom"):
            raise ValidationError("Theory strategy is unsupported")

        # 3. Numeric settings and required explanatory text.
        return Theory(
            id=theory_id, version=version, selected_symbols=tuple(symbols),
            strategy=strategy, lookback_minutes=lookback,
            threshold_pct=setting(data["threshold_pct"], "threshold_pct", "0.01", "50"),
            position_fraction=setting(data["position_fraction"], "position_fraction", "0.001", "1"),
            stop_loss_pct=setting(data["stop_loss_pct"], "stop_loss_pct", "0.01", "100"),
            take_profit_pct=setting(data["take_profit_pct"], "take_profit_pct", "0.01", "100"),
            title=text_field(data["title"], "title", 120),
            hypothesis=text_field(data["hypothesis"], "hypothesis", 4000),
            entry_rule=text_field(data["entry_rule"], "entry_rule", 4000),
            exit_rule=text_field(data["exit_rule"], "exit_rule", 4000),
            risk_rule=text_field(data["risk_rule"], "risk_rule", 4000),
        )
    except (KeyError, TypeError, UnicodeError):
        raise ValidationError("Missing or malformed theory fields") from None


def read_theory(api, assigned_symbol):
    """Read and validate this member's theory and policy eligibility."""
    if assigned_symbol not in SYMBOLS:
        raise ValidationError("Unsupported assigned currency")
    theory = validate_theory(api.get("theory.php"))
    # The API allows multiple symbols, but the assignment requires one per member.
    if theory.selected_symbols != (assigned_symbol,):
        raise ValidationError("Theory must select only this member's assigned currency")
    policy = api.get("policies.php")
    allowed = policy.get("allowed_symbols")
    if not isinstance(allowed, list) or any(not isinstance(symbol, str) for symbol in allowed):
        raise ValidationError("Malformed policy allowed_symbols")
    if assigned_symbol not in allowed:
        raise ValidationError("Assigned currency is not enabled in the security policy")
    return theory
