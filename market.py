"""Public market replay and signals. No orders, forecasts, or model calls."""

import math
import time
from dataclasses import dataclass
from decimal import Decimal, localcontext

from validation import SYMBOLS

PRICE_TOLERANCE = Decimal("0.00000001")


class MarketError(ValueError):
    """Missing or inconsistent market evidence: the trader must HOLD."""


def finite_number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise MarketError("Market parameters must be finite JSON numbers")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise MarketError("Invalid market integer")
    return value


def validate_model(model, symbol):
    """Check the descriptor fields used by the reference formula."""
    try:
        if symbol not in SYMBOLS or not isinstance(model["version"], str) or not model["version"]:
            raise MarketError("Invalid market symbol or model version")
        integer(model["epoch_unix"])
        integer(model["tick_seconds"], 1)
        integer(model["noise_modulus"], 2)
        integer(model["noise_multiplier"], 1)
        if type(model["price_decimals"]) is not int or model["price_decimals"] != 8:
            raise MarketError("Replay expects eight price decimals")
        for name in ("power_timescale_days", "min_price", "max_price"):
            if finite_number(model[name]) <= 0:
                raise MarketError("Market scales and price bounds must be positive")
        if model["min_price"] > model["max_price"]:
            raise MarketError("Invalid price bounds")
        asset = model["assets"][symbol]
        if finite_number(asset["base_price"]) <= 0:
            raise MarketError("Invalid base price")
        for name in ("power_exponent", "phase", "noise_amplitude"):
            finite_number(asset[name])
        integer(asset["noise_seed"])
        cycles = model["cycles"]
        amplitudes = asset["cycle_amplitudes"]
        if not isinstance(cycles, list) or not isinstance(amplitudes, list) or len(cycles) != len(amplitudes):
            raise MarketError("Cycle amplitudes do not match cycles")
        for cycle, amplitude in zip(cycles, amplitudes):
            if finite_number(cycle["period_seconds"]) <= 0:
                raise MarketError("Cycle periods must be positive")
            finite_number(cycle["phase"])
            finite_number(amplitude)
    except (KeyError, TypeError, OverflowError):
        raise MarketError("Missing or malformed market descriptor") from None


def price_at(model, symbol, timestamp, *, quote_time=None):
    """Appendix A formula. quote_time bounds live inputs; omit only in formula tests."""
    validate_model(model, symbol)
    try:
        finite_number(timestamp)
        if timestamp < model["epoch_unix"]:
            raise MarketError("Replay time is before the model epoch")
        if quote_time is not None:
            finite_number(quote_time)
            if timestamp > quote_time:
                raise MarketError("Future samples cannot inform a live decision")

        # 1. Snap to the simulator tick and compute the long-term trend.
        tick = math.floor((timestamp - model["epoch_unix"]) / model["tick_seconds"])
        seconds = tick * model["tick_seconds"]
        asset = model["assets"][symbol]
        ratio = seconds / 86400 / model["power_timescale_days"]
        trend = asset["base_price"] * (1 + ratio) ** asset["power_exponent"]

        # 2. Add waves. Preserve the reference formula's operation order.
        wave = 0
        for amplitude, cycle in zip(asset["cycle_amplitudes"], model["cycles"]):
            angle = 2 * math.pi * (seconds / cycle["period_seconds"])
            angle += asset["phase"]
            angle += cycle["phase"]
            wave += amplitude * math.sin(angle)

        # 3. Noise uses integer arithmetic and TWO multiply/remainder steps.
        modulus = model["noise_modulus"]
        multiplier = model["noise_multiplier"]
        value = (tick + asset["noise_seed"]) % modulus
        value = value * multiplier % modulus
        value = value * multiplier % modulus
        noise = 2 * value / modulus - 1

        # 4. Clamp, then round half up to eight decimal places, as in the guide.
        raw = trend * math.exp(wave + asset["noise_amplitude"] * noise)
        if not math.isfinite(raw):
            raise MarketError("Replay produced a non-finite price")
        clamped = min(model["max_price"], max(model["min_price"], raw))
        return math.floor(clamped * 1e8 + 0.5) / 1e8
    except (OverflowError, ZeroDivisionError):
        raise MarketError("Market formula could not be evaluated") from None


@dataclass(frozen=True)
class Signals:
    timestamp: int
    current_price: Decimal
    replayed_price: Decimal
    lookback_return_pct: Decimal
    mean_deviation_pct: Decimal
    sample_count: int = 61

    def strategy_signal(self, strategy):
        if strategy == "momentum":
            return self.lookback_return_pct
        if strategy == "mean_reversion":
            return -self.mean_deviation_pct
        # Custom rules must be implemented explicitly; do not invent a signal.
        return None


def calculate_signals(model, symbol, quote, lookback_minutes):
    validate_model(model, symbol)
    try:
        now = integer(quote["timestamp_unix"])
        integer(lookback_minutes, 1)
        if lookback_minutes > 1440 or quote["symbol"] != symbol:
            raise MarketError("Invalid lookback or quote symbol")
        if quote["model_version"] != model["version"]:
            raise MarketError("Quote and descriptor versions differ")
        current = Decimal(str(finite_number(quote["price"])))
        if current <= 0:
            raise MarketError("Quote price must be positive")
        start = now - lookback_minutes * 60
        if start < model["epoch_unix"]:
            raise MarketError("Not enough history for the theory lookback")
        # Use the fresh quote time, never a cached replay_end_unix cutoff.
        prices = []
        for index in range(61):
            timestamp = start + (now - start) * index / 60
            prices.append(Decimal(str(price_at(model, symbol, timestamp, quote_time=now))))
        if abs(prices[-1] - current) > PRICE_TOLERANCE:
            raise MarketError("Replayed current price does not match the quote")
        with localcontext() as context:
            context.prec = 50
            average = sum(prices) / len(prices)
            return Signals(now, current, prices[-1],
                           (current / prices[0] - 1) * 100,
                           (current / average - 1) * 100)
    except (KeyError, TypeError, OverflowError, ZeroDivisionError):
        raise MarketError("Missing or malformed quote or replay data") from None


class MarketReader:
    """Reuse one reader across cycles to cache the descriptor for five minutes."""
    def __init__(self, api):
        self.api = api
        self.model = None
        self.cached_at = 0.0

    def refresh_model(self):
        reply = self.api.get("market_model.php")
        model = reply.get("model")
        if not isinstance(model, dict):
            raise MarketError("Expected a market model object")
        self.model = model
        self.cached_at = time.monotonic()

    def read(self, symbol, lookback_minutes):
        reply = self.api.get("market_prices.php", symbols=symbol)
        try:
            matches = [q for q in reply["prices"] if q["symbol"] == symbol]
            if len(matches) != 1:
                raise MarketError("Expected one quote for the assigned currency")
            quote = matches[0]
            integer(quote["timestamp_unix"])
            integer(reply["server_time_unix"])
            if quote["timestamp_unix"] > reply["server_time_unix"]:
                raise MarketError("Quote is ahead of the server clock")
        except (KeyError, TypeError):
            raise MarketError("Malformed market quote response") from None

        if self.model is None or time.monotonic() - self.cached_at >= 300:
            self.refresh_model()
        # A mismatch, including a changed version, gets one refresh then HOLD.
        for attempt in range(2):
            try:
                validate_model(self.model, symbol)
                step = self.model["tick_seconds"]
                if reply["server_time_unix"] - quote["timestamp_unix"] > 2 * step:
                    raise MarketError("Quote is stale relative to the server clock")
                return calculate_signals(self.model, symbol, quote, lookback_minutes)
            except MarketError:
                if attempt == 1:
                    raise
                self.refresh_model()
