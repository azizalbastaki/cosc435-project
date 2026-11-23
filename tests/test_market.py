import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from client import Cooldown
from market import MarketError, MarketReader, calculate_signals, price_at

MODEL = json.loads((Path(__file__).parent / "fixtures" / "market_model.json").read_text())


def quote_for(model, symbol="ETH", day=30):
    timestamp = model["epoch_unix"] + day * 86400
    return {"symbol": symbol, "price": price_at(model, symbol, timestamp),
            "timestamp_unix": timestamp, "model_version": model["version"]}


def response(quote):
    return {"prices": [quote], "server_time_unix": quote["timestamp_unix"] + 1}


class MarketTests(unittest.TestCase):
    def test_published_btc_full_quote_fixtures(self):
        # Future day 365 is synthetic formula verification, never a live signal.
        for day, expected in [(0, "67000.36767791"), (30, "69404.90274610"), (365, "91475.89718627")]:
            actual = price_at(MODEL, "BTC", MODEL["epoch_unix"] + day * 86400)
            self.assertLessEqual(abs(Decimal(str(actual)) - Decimal(expected)), Decimal("1e-8"))

    def test_baseline_fixtures_without_waves_or_noise(self):
        model = copy.deepcopy(MODEL)
        asset = model["assets"]["BTC"]
        asset["cycle_amplitudes"] = [0] * len(model["cycles"])
        asset["noise_amplitude"] = 0
        for day, expected in [(0, "64000.00000000"), (365, "87426.57643228")]:
            actual = price_at(model, "BTC", model["epoch_unix"] + day * 86400)
            self.assertLessEqual(abs(Decimal(str(actual)) - Decimal(expected)), Decimal("1e-8"))

    def test_time_bounds_and_tick_snapping(self):
        epoch = MODEL["epoch_unix"]
        with self.assertRaises(MarketError):
            price_at(MODEL, "ETH", epoch - 1)
        with self.assertRaises(MarketError):
            price_at(MODEL, "ETH", epoch + 10, quote_time=epoch + 5)
        self.assertEqual(price_at(MODEL, "ETH", epoch), price_at(MODEL, "ETH", epoch + 0.5))

    def test_sixty_one_samples_and_hand_calculated_signals(self):
        model = copy.deepcopy(MODEL)
        asset = model["assets"]["ETH"]
        asset.update(base_price=100, power_exponent=1, cycle_amplitudes=[0] * len(model["cycles"]), noise_amplitude=0)
        model["power_timescale_days"] = 1
        quote = quote_for(model, day=1)
        signals = calculate_signals(model, "ETH", quote, 1440)
        self.assertEqual(signals.sample_count, 61)
        self.assertEqual(signals.lookback_return_pct, Decimal(100))
        self.assertAlmostEqual(float(signals.mean_deviation_pct), 100 / 3, places=7)
        self.assertEqual(signals.strategy_signal("momentum"), Decimal(100))
        self.assertEqual(signals.strategy_signal("mean_reversion"), -signals.mean_deviation_pct)
        self.assertIsNone(signals.strategy_signal("custom"))

    def test_old_descriptor_end_does_not_cut_off_new_quote(self):
        model = dict(MODEL, replay_end_unix=MODEL["epoch_unix"])
        self.assertEqual(calculate_signals(model, "ETH", quote_for(model), 10).sample_count, 61)

    def test_missing_history_and_malformed_inputs(self):
        with self.assertRaises(MarketError):
            calculate_signals(MODEL, "ETH", quote_for(MODEL, day=0), 10)
        for value in (True, "1", float("nan"), float("inf")):
            with self.assertRaises(MarketError):
                price_at(MODEL, "ETH", value)
        for changes in [{"tick_seconds": 0}, {"noise_modulus": True}, {"cycles": []},
                        {"price_decimals": 2}, {"min_price": -1}]:
            with self.assertRaises(MarketError):
                price_at(MODEL | changes, "ETH", MODEL["epoch_unix"])

    def test_mismatch_refresh_once_then_fail(self):
        quote = quote_for(MODEL)
        quote["price"] += 1
        api = Mock()
        api.get.side_effect = [response(quote), {"model": MODEL}, {"model": MODEL}]
        with self.assertRaises(MarketError):
            MarketReader(api).read("ETH", 10)
        self.assertEqual(api.get.call_count, 3)

    def test_version_refresh_recovers_and_cache_reused(self):
        api = Mock()
        new_model = dict(MODEL, version="new_version")
        quote = quote_for(new_model)
        reader = MarketReader(api)
        reader.model = MODEL
        reader.cached_at = 100
        api.get.side_effect = [response(quote), {"model": new_model}, response(quote)]
        with patch("market.time.monotonic", return_value=100):
            reader.read("ETH", 10)
            reader.read("ETH", 10)
        self.assertEqual(api.get.call_count, 3)

    def test_cache_expiry_and_cooldown(self):
        api = Mock()
        reader = MarketReader(api)
        reader.model = MODEL
        reader.cached_at = 0
        api.get.side_effect = [response(quote_for(MODEL)), {"model": MODEL}]
        with patch("market.time.monotonic", return_value=301):
            reader.read("ETH", 10)
        self.assertEqual(api.get.call_count, 2)
        api.get.side_effect = Cooldown(15)
        with self.assertRaises(Cooldown):
            reader.read("ETH", 10)

    def test_stale_future_and_missing_quote_fail(self):
        quote = quote_for(MODEL)
        for reply in [response(quote) | {"server_time_unix": quote["timestamp_unix"] - 1},
                      {"prices": [], "server_time_unix": quote["timestamp_unix"]}]:
            api = Mock()
            api.get.return_value = reply
            with self.assertRaises(MarketError):
                MarketReader(api).read("ETH", 10)
        api = Mock()
        reply = response(quote) | {"server_time_unix": quote["timestamp_unix"] + 100}
        api.get.side_effect = [reply, {"model": MODEL}, {"model": MODEL}]
        with self.assertRaises(MarketError):
            MarketReader(api).read("ETH", 10)


if __name__ == "__main__":
    unittest.main()
