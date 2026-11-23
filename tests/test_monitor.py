import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from client import ApiError
from model_adapter import ModelError
from monitor import main, watch
from validation import ValidationError


class MonitorTests(unittest.TestCase):
    def test_main_reports_safe_failure_and_closes_clients(self):
        for error, expected in [(ApiError(403, "SECRET"), "HTTP 403"),
                                (ModelError("SECRET"), "Muse"),
                                (ValidationError("SECRET", "insufficient_holdings"),
                                 "insufficient_holdings")]:
            with patch("monitor.ledger_config", return_value={"LEDGER_BASE_URL": "https://example.test",
                                                              "TRADER_API_KEY": "fake"}), \
                    patch("monitor.LedgerClient") as client, \
                    patch("monitor.MuseAdapter.from_config") as model, \
                    patch("monitor.watch", side_effect=error) as watch_mock, \
                    patch("builtins.print") as output:
                self.assertEqual(main(["--cycles", "1"]), 1)
                message = output.call_args.args[0]
                self.assertIn(expected, message)
                self.assertNotIn("SECRET", message)
                watch_mock.assert_called_once()
                client.return_value.close.assert_called_once()
                model.return_value.close.assert_called_once()
                client.return_value.post.assert_not_called()

    def test_bounded_checks_wait_only_between_cycles(self):
        api, model = Mock(), Mock()
        order = SimpleNamespace(action="HOLD", quantity=Decimal(0))
        theory = SimpleNamespace(strategy="momentum", lookback_minutes=10,
                                 threshold_pct=Decimal("0.5"))
        signals = Mock(timestamp=1000)
        signals.strategy_signal.return_value = Decimal("-0.7")
        with patch("monitor.decide_live", return_value=(order, theory, signals)) as decide, \
                patch("monitor.time.sleep") as sleep, patch("builtins.print"):
            watch(api, model, "ETH", 3, 60)
        self.assertEqual(decide.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(60)
        api.post.assert_not_called()

    def test_failure_stops_instead_of_repeating(self):
        with patch("monitor.decide_live", side_effect=ValueError("invalid")) as decide, \
                patch("monitor.time.sleep") as sleep:
            with self.assertRaises(ValueError):
                watch(Mock(), Mock(), "ETH", 3, 60)
        self.assertEqual(decide.call_count, 1)
        sleep.assert_not_called()
