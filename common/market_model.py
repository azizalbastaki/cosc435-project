"""Pure-Python replica of Ledger's public market model `ledger-powerlaw-v1`.

Mirrors assets/js/market-model.js on the Ledger site. Used by the Trading Agent
(signals, price replay, data-integrity checks) and by the Guardian (independent
price recheck). Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import math

SUPPORTED_VERSION = "ledger-powerlaw-v1"


class ModelError(ValueError):
    pass


def validate(model: dict) -> dict:
    """Reject anything that does not look exactly like the published model."""
    try:
        ok = (model["version"] == SUPPORTED_VERSION and model["simulated"] is True
              and isinstance(model["tick_seconds"], int) and 1 <= model["tick_seconds"] <= 60
              and model["noise_modulus"] == 2147483647 and model["noise_multiplier"] == 48271
              and model["power_timescale_days"] > 0 and len(model["cycles"]) == 3)
    except (KeyError, TypeError) as exc:
        raise ModelError(f"market model is missing fields: {exc}") from exc
    if not ok:
        raise ModelError("unsupported market model")
    for c in model["cycles"]:
        if not (c["period_seconds"] > 0 and math.isfinite(c["phase"])):
            raise ModelError("invalid cycle")
    for sym, a in model["assets"].items():
        for k in ("base_price", "power_exponent", "phase", "noise_amplitude", "noise_seed"):
            if not math.isfinite(float(a[k])):
                raise ModelError(f"invalid asset field {sym}.{k}")
        if a["base_price"] <= 0 or len(a["cycle_amplitudes"]) != 3:
            raise ModelError(f"invalid asset {sym}")
    return model


def fingerprint(model: dict, symbol: str) -> str:
    """SHA-256 of the parameters that determine `symbol`'s price.

    Time fields (server_time_unix, current_tick, replay_end_unix) change on every
    call, so they are excluded. A change in this hash means the price formula itself
    changed: the agent treats that as untrusted data until a human re-pins it.
    """
    core = {k: model[k] for k in ("version", "epoch_unix", "tick_seconds", "power_timescale_days",
                                  "noise_modulus", "noise_multiplier", "cycles", "min_price", "max_price")}
    core["asset"] = model["assets"][symbol]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def tick_at(model: dict, unix_seconds: float) -> int:
    return max(0, math.floor((unix_seconds - model["epoch_unix"]) / model["tick_seconds"]))


def trend_at_tick(model: dict, symbol: str, tick: int) -> float:
    a = model["assets"][symbol]
    days = tick * model["tick_seconds"] / 86400
    return a["base_price"] * (1 + days / model["power_timescale_days"]) ** a["power_exponent"]


def _bound(model: dict, value: float) -> float:
    v = min(model["max_price"], max(model["min_price"], value))
    return math.floor(v * 1e8 + 0.5) / 1e8


def quote_at_tick(model: dict, symbol: str, tick: int) -> float:
    a = model["assets"][symbol]
    m, k = model["noise_modulus"], model["noise_multiplier"]
    x = (tick + a["noise_seed"]) % m
    x = (x * k) % m
    x = (x * k) % m
    noise = 2 * x / m - 1
    seconds = tick * model["tick_seconds"]
    wave = 0.0
    for amp, cyc in zip(a["cycle_amplitudes"], model["cycles"]):
        wave += amp * math.sin(2 * math.pi * (seconds / cyc["period_seconds"]) + a["phase"] + cyc["phase"])
    return _bound(model, trend_at_tick(model, symbol, tick) * math.exp(wave + a["noise_amplitude"] * noise))


def quote(model: dict, symbol: str, unix_seconds: float) -> float:
    return quote_at_tick(model, symbol, tick_at(model, unix_seconds))


def trend(model: dict, symbol: str, unix_seconds: float) -> float:
    return _bound(model, trend_at_tick(model, symbol, tick_at(model, unix_seconds)))


def minute_samples(model: dict, symbol: str, end_unix: float, minutes: int) -> list[float]:
    """1-minute samples of the price for the `minutes` minutes ending at `end_unix` (inclusive)."""
    end = int(end_unix)
    return [quote(model, symbol, end - 60 * i) for i in range(minutes - 1, -1, -1)]
