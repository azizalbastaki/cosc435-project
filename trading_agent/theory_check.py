"""Theory validation: is the theory we are about to act on the one we validated, and
does the live market still behave the way the theory assumes?"""
from __future__ import annotations

from common import market_model as mm


class TheoryInvalid(Exception):
    pass


SUPPORTED = ("BTC", "ETH", "DOGE", "XMR", "XRP", "SOL")


REQUIRED = ("id", "version", "status", "strategy", "selected_symbols", "lookback_minutes",
            "threshold_pct", "position_fraction", "stop_loss_pct", "take_profit_pct")


def validate_structure(theory: dict) -> dict:
    """Validate the Ledger contract without applying local trading eligibility."""
    if not isinstance(theory, dict):
        raise TheoryInvalid("expected a theory object")
    if not theory:
        raise TheoryInvalid("no active theory: the agent holds until one is submitted")
    missing = [k for k in REQUIRED if theory.get(k) in (None, "")]
    if missing:
        raise TheoryInvalid(f"theory missing fields {missing}")
    if theory["status"] != "SUBMITTED":
        raise TheoryInvalid(f"theory status is {theory['status']}, not SUBMITTED")
    # guide 6.3: structural checks before the theory is used at all
    for k in ("id", "version", "lookback_minutes"):
        if isinstance(theory[k], bool) or not isinstance(theory[k], int) or theory[k] <= 0:
            raise TheoryInvalid(f"{k} must be a positive integer")
    if theory["lookback_minutes"] > 1440:
        raise TheoryInvalid("lookback cannot exceed 1,440 minutes")
    if len(str(theory.get("title", "")).encode()) > 120:
        raise TheoryInvalid("title exceeds 120 UTF-8 bytes")
    for k in ("title", "hypothesis", "entry_rule", "exit_rule", "risk_rule"):
        v = str(theory.get(k) or "")
        if "\x00" in v:
            raise TheoryInvalid(f"{k} contains a NUL character")
        if k != "title" and len(v.encode()) > 4000:
            raise TheoryInvalid(f"{k} exceeds 4,000 UTF-8 bytes")
    syms = theory["selected_symbols"]
    if not isinstance(syms, list) or not 1 <= len(syms) <= 6 or any(x not in SUPPORTED for x in syms):
        raise TheoryInvalid("theory must select 1-6 supported currencies")
    if theory["strategy"] not in ("momentum", "mean_reversion", "custom"):
        raise TheoryInvalid(f"unrecognised strategy {theory['strategy']!r}")
    ranges = {"threshold_pct": (0.01, 50), "position_fraction": (0.001, 1), "stop_loss_pct": (0.01, 100),
              "take_profit_pct": (0.01, 100)}
    for k, (lo, hi) in ranges.items():
        v = theory[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi \
                or round(v, 4) != v:
            raise TheoryInvalid(f"{k}={v!r} outside the documented range [{lo}, {hi}] or >4 decimals")
    return theory


def validate_theory(theory: dict, cfg: dict) -> dict:
    """Validate the contract, local safety limits, and expected theory pins."""
    validate_structure(theory)
    exp = cfg["expected_theory"]
    if exp.get("version") and int(theory["version"]) != int(exp["version"]):
        raise TheoryInvalid(f"theory version {theory['version']} differs from validated version {exp['version']};"
                            " re-run replay validation and update config before trading")
    if theory["strategy"] not in ("mean_reversion", "momentum"):
        raise TheoryInvalid(f"strategy {theory['strategy']} is not implemented by this agent")
    if exp.get("strategy") and theory["strategy"] != exp["strategy"]:
        raise TheoryInvalid("active strategy differs from the validated strategy pin")
    sym = cfg["symbol"]
    if sym not in theory["selected_symbols"]:
        raise TheoryInvalid(f"{sym} is not selected in the theory")
    bounds = {"position_fraction": (0.001, 0.2),
              "stop_loss_pct": (1, 20), "take_profit_pct": (5, 100)}
    if theory["strategy"] == "mean_reversion":
        bounds.update(lookback_minutes=(60, 1440), threshold_pct=(1, 20))
    for k, (lo, hi) in bounds.items():
        v = float(theory[k])
        if not lo <= v <= hi:
            raise TheoryInvalid(f"{k}={v} outside the validated safe range [{lo}, {hi}]")
    # the hypothesis relies on the lookback spanning whole cycles (10 min, 2 h, 24 h)
    if theory["strategy"] == "mean_reversion" and int(theory["lookback_minutes"]) % 120:
        raise TheoryInvalid("lookback must be a whole number of 2-hour cycles for the 24h-mean hypothesis")
    for k, v in exp.get("params", {}).items():
        if abs(float(theory[k]) - float(v)) > 1e-9:
            raise TheoryInvalid(f"theory {k}={theory[k]} differs from validated value {v}")
    return theory


def validate_model(model: dict, symbol: str, pinned: str | None) -> str:
    mm.validate(model)
    if symbol not in model["assets"]:
        raise TheoryInvalid(f"{symbol} not in market model")
    fp = mm.fingerprint(model, symbol)
    if pinned and fp != pinned:
        raise TheoryInvalid("market model parameters changed since validation (fingerprint mismatch):"
                            " possible tampering or a model update; human must re-validate")
    return fp


def market_data_checks(samples: list[float], max_jump_pct: float) -> list[str]:
    """Sanity checks the model can never fail; a failure means the data is not the model's."""
    problems = []
    if len(samples) < 2 or any(p <= 0 for p in samples):
        problems.append("missing or non-positive samples")
        return problems
    five_min = abs(samples[-1] / samples[-6] - 1) * 100 if len(samples) >= 6 else 0
    if five_min > max_jump_pct:
        problems.append(f"price moved {five_min:.2f}% in 5 minutes (> {max_jump_pct}%)")
    return problems


def server_price_deviation_pct(server_price: float, model_price: float) -> float:
    """Return deviation % between the price the server quoted and the model's price."""
    return abs(server_price / model_price - 1) * 100


def replay_matches(model: dict, symbol: str, quote: dict) -> bool:
    """Guide A.2 step 4: the replayed price at the quote's timestamp must equal the live quote."""
    return abs(mm.quote(model, symbol, quote["timestamp_unix"]) - float(quote["price"])) <= 1e-8
