import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from client import ApiError, Cooldown, LedgerClient
from configuration import guardian_config, ledger_config
from guardian_check import main
from guardian_source import GuardianSource
from test_smoke import MARKET, POLICY, PORTFOLIO, response


class GuardianSourceTests(unittest.TestCase):
    def setUp(self):
        self.row = {"transaction_id": 1, "symbol": "ETH", "action": "BUY",
                    "requested_quantity": 0.1, "requested_price": 1,
                    "estimated_total": 0.1, "reasoning": "DO NOT PRINT THIS"}
        self.api = Mock(role="guardian")

    def test_independent_evidence_and_no_queue_context(self):
        self.api.get.side_effect = [
            {"pending": [self.row], "has_more": True, "context": {"policy": "untrusted"}},
            POLICY, PORTFOLIO, MARKET,
        ]
        snapshots, more = GuardianSource(self.api).collect()
        self.assertTrue(more)
        self.assertEqual(len(snapshots), 1)
        self.assertIs(snapshots[0].policy, POLICY)
        self.assertIs(snapshots[0].market, MARKET)
        self.assertEqual(self.api.get.call_args_list, [call("trade_pending.php", limit=2),
            call("policies.php"), call("portfolio.php"), call("market_prices.php", symbols="ETH")])
        self.assertNotIn("DO NOT PRINT", repr(snapshots))

    def test_two_orders_each_get_fresh_evidence(self):
        self.api.get.side_effect = [{"pending": [self.row, self.row | {"transaction_id": 2}], "has_more": True},
                                    POLICY, PORTFOLIO, MARKET, POLICY, PORTFOLIO, MARKET]
        snapshots, _ = GuardianSource(self.api).collect()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(self.api.get.call_count, 7)

    def test_empty_queue(self):
        self.api.get.return_value = {"pending": [], "has_more": False}
        self.assertEqual(GuardianSource(self.api).collect(), ([], False))
        self.api.get.assert_called_once_with("trade_pending.php", limit=2)

    def test_malformed_pending_stops_before_evidence(self):
        for row in [self.row | {"transaction_id": True}, self.row | {"symbol": "BAD"},
                    self.row | {"requested_quantity": "0.1"}, self.row | {"requested_quantity": -1},
                    self.row | {"action": "WITHDRAW"}]:
            self.api.reset_mock()
            self.api.get.return_value = {"pending": [row], "has_more": False}
            with self.assertRaises(ValueError):
                GuardianSource(self.api).collect()
            self.assertEqual(self.api.get.call_count, 1)

    def test_cooldown_propagates_without_more_requests(self):
        self.api.get.side_effect = [{"pending": [self.row], "has_more": False}, Cooldown(15)]
        with self.assertRaises(Cooldown):
            GuardianSource(self.api).collect()
        self.assertEqual(self.api.get.call_count, 2)

    def test_role_restriction_and_no_writes(self):
        with patch("client.requests.Session") as session:
            guardian = LedgerClient("https://ledger.chanyeun.com/api/v1", "fake_guardian", role="guardian")
            session.return_value.request.return_value = response({"pending": [], "has_more": False})
            guardian.get("trade_pending.php", limit=2)
            request = session.return_value.request.call_args
            self.assertEqual(request.kwargs["headers"], {"X-Api-Key": "fake_guardian"})
            self.assertEqual(request.args[0], "GET")
            for endpoint in ("trade_review.php", "trade_propose.php"):
                with self.assertRaises(ValueError):
                    guardian.request("POST", endpoint)
            trader = LedgerClient("https://ledger.chanyeun.com/api/v1", "fake_trader")
            with self.assertRaises(ValueError):
                trader.get("trade_pending.php")
            with self.assertRaises(ValueError):
                GuardianSource(trader)
            guardian.next_request_at = 0
            session.return_value.request.return_value = response({"error": "insufficient_scope"}, 403)
            with self.assertRaisesRegex(ApiError, "guardian-scoped"):
                guardian.get("trade_pending.php")

    def test_separate_role_config_and_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.guardian.json"
            config = {"LEDGER_BASE_URL": "https://ledger.chanyeun.com/api/v1", "GUARDIAN_API_KEY": "fake_guardian"}
            path.write_text(json.dumps(config))
            self.assertEqual(guardian_config(path), config)
            with self.assertRaises(ValueError):
                ledger_config(path)
            with patch("guardian_check.LedgerClient", return_value=self.api):
                self.api.get.side_effect = [{"pending": [self.row], "has_more": False}, POLICY, PORTFOLIO, MARKET]
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--config", str(path)]), 0)
                self.assertIn("Order #1", output.getvalue())
                self.assertNotIn("fake_guardian", output.getvalue())
                self.assertNotIn("DO NOT PRINT", output.getvalue())
                self.api.close.assert_called_once()
            config["TRADER_API_KEY"] = "fake_trader"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                guardian_config(path)


if __name__ == "__main__":
    unittest.main()
