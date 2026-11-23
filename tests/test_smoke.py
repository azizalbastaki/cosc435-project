import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from client import ApiError, Cooldown, LedgerClient, retry_seconds
from smoke_test import load_config, main, run, summary

BASE = "https://ledger.chanyeun.com/api/v1"
KEY = "test_secret_never_display"
PORTFOLIO = {"cash_balance": 100000, "holdings": [], "holdings_value": 0, "total_value": 100000}
POLICY = {
    "max_transaction_amount": 10000, "max_trades_per_hour": 10,
    "max_trades_per_day": 50, "restricted_actions": ["WITHDRAW"],
    "escalation_threshold": 50000, "allowed_symbols": ["ETH", "SOL"],
    "trades_last_hour": 0, "trades_last_day": 0,
}
MARKET = {"prices": [{"symbol": "ETH", "price": 3200.12345678,
                      "change_pct": 0.1, "updated_at": "2026-11-20 12:00:00",
                      "timestamp_unix": 1795176000, "tick": 1, "model_version": "v1"}],
          "simulated": True, "model_version": "v1", "server_time_unix": 1795176000}


def response(body=None, status=200, headers=None):
    result = Mock(status_code=status, ok=200 <= status < 400, headers=headers or {})
    result.json.return_value = body
    return result


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch("client.requests.Session")
        self.session = self.session_patch.start().return_value
        self.addCleanup(self.session_patch.stop)
        self.clock = patch("client.time.monotonic", return_value=100)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sleep_patch = patch("client.time.sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.api = LedgerClient(BASE, KEY)

    def test_three_gets_and_summary(self):
        self.session.request.side_effect = [response(PORTFOLIO), response(POLICY), response(MARKET)]
        output = run(self.api, "ETH")
        self.assertIn("ETH held 0", output)
        self.assertIn("3,200.12345678", output)
        self.assertNotIn(KEY, output)
        self.assertNotIn("X-Api-Key", output)
        calls = self.session.request.call_args_list
        self.assertEqual([c.args for c in calls], [
            ("GET", BASE + "/portfolio.php"), ("GET", BASE + "/policies.php"),
            ("GET", BASE + "/market_prices.php")])
        self.assertEqual(calls[-1].kwargs["params"], {"symbols": "ETH"})
        for call in calls:
            self.assertEqual(call.kwargs["headers"], {"X-Api-Key": KEY})
            self.assertEqual(call.kwargs["timeout"], (5, 15))
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(self.session.trust_env)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertAlmostEqual(self.sleep.call_args.args[0], 1.1)

    def test_other_symbol_and_owned_holding(self):
        portfolio = copy.deepcopy(PORTFOLIO)
        portfolio["holdings"] = [{"symbol": "SOL", "quantity": 2.5}]
        market = {"prices": [{"symbol": "SOL", "price": 150}]}
        self.session.request.side_effect = [response(portfolio), response(POLICY), response(market)]
        self.assertIn("SOL held 2.5", run(self.api, "SOL"))
        self.assertEqual(self.session.request.call_args.kwargs["params"], {"symbols": "SOL"})

    def test_no_writes_or_other_endpoints(self):
        for method, endpoint in [("POST", "portfolio.php"), ("GET", "trade_propose.php"),
                                 ("GET", "transactions.php"), ("GET", "https://other.test")]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.api.request(method, endpoint)
        self.session.request.assert_not_called()

    def test_invalid_urls_and_keys(self):
        for base in ["http://example.test", "https://u:p@example.test", BASE + "?",
                     BASE + "#x", "https://example.test:bad", "https://example.test\n"]:
            with self.subTest(base=base), self.assertRaises(ValueError):
                LedgerClient(base, KEY)
        for key in ["", "PASTE_KEY", " abc", "abc\r\nInjected: x"]:
            with self.assertRaises(ValueError):
                LedgerClient(BASE, key)

    def test_redirect_and_html_are_not_exposed(self):
        self.session.request.return_value = response({"secret": KEY}, 302)
        with self.assertRaisesRegex(ApiError, "Redirect refused"):
            self.api.get("portfolio.php")
        reply = response(status=502)
        reply.json.side_effect = ValueError(KEY)
        self.session.request.return_value = reply
        with self.assertRaisesRegex(ApiError, "hosting layer") as error:
            self.api.get("portfolio.php")
        self.assertNotIn(KEY, str(error.exception))

    def test_api_errors_and_bad_shapes(self):
        for body, status in [({"error": "insufficient_scope", "message": KEY}, 403),
                             ({"error": KEY, "message": KEY}, 401), ([], 200)]:
            self.session.request.return_value = response(body, status)
            with self.assertRaises(ApiError) as error:
                self.api.get("portfolio.php")
            self.assertEqual(error.exception.status, status)
            self.assertNotIn(KEY, str(error.exception))
        self.session.request.return_value = response({"error": "insufficient_scope"}, 403)
        with self.assertRaisesRegex(ApiError, "insufficient_scope.*trader-scoped"):
            self.api.get("portfolio.php")

    def test_cooldown_blocks_next_endpoint(self):
        for status in (429, 503):
            with self.subTest(status=status):
                api = LedgerClient(BASE, KEY)
                self.session.request.reset_mock()
                self.session.request.return_value = response(status=status, headers={"Retry-After": "20"})
                with self.assertRaises(Cooldown) as error:
                    api.get("portfolio.php")
                self.assertGreaterEqual(error.exception.seconds, 20)
                with self.assertRaises(Cooldown):
                    api.get("policies.php")
                self.assertEqual(self.session.request.call_count, 1)

    def test_retry_after_date_and_fallback(self):
        with patch("client.time.time", return_value=0):
            self.assertEqual(retry_seconds("Thu, 01 Jan 1970 00:01:00 GMT"), 60)
        self.assertEqual(retry_seconds("invalid"), 15)

    def test_network_retries_bounded(self):
        self.session.request.side_effect = requests.ConnectionError(KEY)
        with self.assertRaises(ApiError) as error:
            self.api.get("portfolio.php")
        self.assertEqual(self.session.request.call_count, 3)
        self.assertNotIn(KEY, str(error.exception))

    def test_summary_rejects_malformed_fields(self):
        for price in [True, "3200", float("nan"), float("inf"), -1, 0]:
            with self.subTest(price=price), self.assertRaises(ValueError):
                summary(PORTFOLIO, POLICY, {"prices": [{"symbol": "ETH", "price": price}]}, "ETH")
        with self.assertRaises(ValueError):
            summary(PORTFOLIO, POLICY, {"prices": []}, "ETH")
        policy = dict(POLICY, allowed_symbols=[])
        self.assertIn("allowed=no", summary(PORTFOLIO, policy, MARKET, "ETH"))

    def test_config_and_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in [{"TRADER_API_KEY": KEY}, {"LEDGER_BASE_URL": BASE, "TRADER_API_KEY": 12},
                          {"LEDGER_BASE_URL": BASE, "TRADER_API_KEY": KEY, "GUARDIAN_API_KEY": KEY}]:
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text("x" * 8193)
            with self.assertRaises(ValueError):
                load_config(path)
            path.write_text(json.dumps({"LEDGER_BASE_URL": BASE, "TRADER_API_KEY": KEY}))
            self.session.request.side_effect = [response(PORTFOLIO), response(POLICY), response(MARKET)]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--config", str(path), "--symbol", "eth"]), 0)
            self.assertNotIn(KEY, stdout.getvalue())
            self.session.close.assert_called_once()
            self.session.request.side_effect = None
            self.session.request.return_value = response(status=429)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(["--config", str(path)]), 2)
            self.assertIn("wait at least", stderr.getvalue())
            self.assertNotIn(KEY, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
