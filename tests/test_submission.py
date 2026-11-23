import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from client import ApiError, Cooldown, LedgerClient, UncertainWrite
from offline_demo import FixtureSource, ScriptedModel
from recovery import ProposalJournal, RecoveryError
from test_smoke import response
from test_theory import THEORY
from theory import validate_theory
from trade_once import main, reconcile, submit_once
from validation import ValidatedOrder


class WriteTransportTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch("client.requests.Session")
        self.session = self.session_patch.start().return_value
        self.addCleanup(self.session_patch.stop)
        self.sleep = patch("client.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.api = LedgerClient("https://ledger.chanyeun.com/api/v1", "fake_key", allow_writes=True)

    def test_explicit_opt_in_role_and_body(self):
        readonly = LedgerClient(self.api.base, "fake_key")
        with self.assertRaises(ValueError):
            readonly.post("trade_propose.php", {})
        for endpoint, body in [("trade_review.php", {}), ("trade_propose.php", []),
                               ("trade_propose.php", {"q": float("nan")}),
                               ("trade_propose.php", {"text": "x" * 32768})]:
            with self.assertRaises(ValueError):
                self.api.post(endpoint, body)
        self.session.request.assert_not_called()
        self.session.request.return_value = response({"status": "PENDING"}, 201)
        self.api.post("trade_propose.php", {"quantity": "0.1"})
        sent = self.session.request.call_args
        self.assertEqual(sent.args[0], "POST")
        self.assertEqual(sent.kwargs["headers"]["Content-Type"], "application/json")
        self.assertFalse(sent.kwargs["allow_redirects"])
        guardian = LedgerClient(self.api.base, "guardian_key", role="guardian", allow_writes=True)
        with self.assertRaises(ValueError):
            guardian.post("trade_propose.php", {})

    def test_uncertain_writes_never_retry(self):
        for result in [requests.ConnectionError("fake_key"), response({}, 500), response({}, 503), response([], 200)]:
            self.session.request.reset_mock()
            self.session.request.side_effect = [result]
            self.api.next_allowed = 0
            with self.assertRaises(UncertainWrite) as error:
                self.api.post("trade_propose.php", {})
            self.assertNotIn("fake_key", str(error.exception))
            self.assertEqual(self.session.request.call_count, 1)

    def test_html_redirect_and_cooldown(self):
        html = response(status=200)
        html.json.side_effect = ValueError("fake_key")
        self.session.request.return_value = html
        with self.assertRaises(UncertainWrite):
            self.api.post("trade_review.php" if self.api.role == "guardian" else "trade_propose.php", {})
        self.session.request.return_value = response({}, 302)
        with self.assertRaises(ApiError):
            self.api.post("trade_propose.php", {})
        self.session.request.return_value = response({}, 429, {"Retry-After": "15"})
        with self.assertRaises(Cooldown):
            self.api.post("trade_propose.php", {})
        with self.assertRaises(Cooldown):
            self.api.get("portfolio.php")


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "journal.json"
        self.journal = ProposalJournal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.theory = validate_theory({"theory": THEORY})
        self.order = ValidatedOrder("ETH", "BUY", Decimal("0.1"), "Signal checked", Decimal(320), Decimal(320))
        self.trader = Mock()
        self.guardian = Mock()
        self.records = []
        self.trader.get.side_effect = lambda endpoint, **kwargs: {"transactions": copy.deepcopy(self.records)}
        self.guardian.get.return_value = {"pending": []}
        self.row = {"transaction_id": 42, "symbol": "ETH", "action": "BUY", "requested_quantity": 0.1,
                    "reasoning": "Sample candidate", "theory_id": 7, "theory_version": 2, "status": "PENDING"}
        def propose(endpoint, body):
            self.records[:] = [dict(self.row)]
            return {"transaction_id": 42, "status": "PENDING", "symbol": "ETH", "action": "BUY", "theory_id": 7, "theory_version": 2}
        def review(endpoint, body):
            self.records[0].update(status="EXECUTED", executed_quantity=0.1, executed_price=3200, executed_total=320)
            return {"transaction_id": 42, "status": "EXECUTED"}
        self.trader.post.side_effect = propose
        self.guardian.post.side_effect = review
        self.source = FixtureSource([self.row])
        self.source.order = self.row
        self.model = ScriptedModel({"decision": "APPROVE", "reason": "Checks passed", "risk_score": 5,
                                    "modified_quantity": None, "rules_triggered": []})
        for target, kwargs in [
            ("trade_once.read_theory", {"return_value": self.theory}),
            ("trade_once.decide_live", {"side_effect": lambda *args: (self.order, self.theory, None)}),
            ("trade_once.recheck_candidate", {"side_effect": lambda api, order, theory: order}),
            ("trade_once.LiveGuardianSource", {"return_value": self.source}),
        ]:
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cycle(self):
        return submit_once(self.trader, self.guardian, Mock(), self.model, self.journal, "ETH")

    def test_one_proposal_one_review_and_confirmed_fill(self):
        self.assertIn("EXECUTED", self.run_cycle())
        self.trader.post.assert_called_once()
        self.guardian.post.assert_called_once()
        self.assertIsNone(self.journal.open_proposal())
        self.assertEqual(self.trader.post.call_args.args[0], "trade_propose.php")
        self.assertEqual(self.guardian.post.call_args.args[0], "trade_review.php")

    def test_hold_never_writes_or_reserves(self):
        self.order = ValidatedOrder("ETH", "HOLD", Decimal(0), "No signal", Decimal(0), Decimal(0))
        self.assertIn("HOLD", self.run_cycle())
        self.trader.post.assert_not_called()
        self.guardian.post.assert_not_called()
        self.assertIsNone(self.journal.open_proposal())

    def test_existing_pending_or_theory_mismatch_stops_new_trade(self):
        self.records[:] = [self.row]
        with self.assertRaises(RecoveryError):
            self.run_cycle()
        self.trader.post.assert_not_called()
        self.records.clear()
        with patch("trade_once.read_theory", side_effect=[self.theory, validate_theory({"theory": THEORY | {"id": 8}})]):
            with self.assertRaises(RecoveryError):
                self.run_cycle()
        self.trader.post.assert_not_called()

    def test_lost_proposal_response_blocks_restart(self):
        self.trader.post.side_effect = UncertainWrite("Lost response")
        with self.assertRaises(UncertainWrite):
            self.run_cycle()
        self.journal.close()
        self.journal = ProposalJournal(self.path)
        with self.assertRaises(RecoveryError):
            self.run_cycle()
        self.assertEqual(self.trader.post.call_count, 1)
        self.guardian.post.assert_not_called()

    def test_lost_review_response_reconciles_execution_without_resend(self):
        original = self.guardian.post.side_effect
        def lost(endpoint, body):
            original(endpoint, body)
            raise UncertainWrite("Lost review response")
        self.guardian.post.side_effect = lost
        with self.assertRaises(UncertainWrite):
            self.run_cycle()
        self.assertIn("EXECUTED", self.run_cycle())
        self.assertEqual(self.trader.post.call_count, 1)
        self.assertEqual(self.guardian.post.call_count, 1)

    def test_lost_review_still_pending_does_not_retry(self):
        self.guardian.post.side_effect = UncertainWrite("Lost response")
        with self.assertRaises(UncertainWrite):
            self.run_cycle()
        with self.assertRaises(RecoveryError):
            self.run_cycle()
        self.assertEqual(self.guardian.post.call_count, 1)
        self.assertEqual(self.trader.post.call_count, 1)

    def test_no_history_match_keeps_gate_closed(self):
        intent = self.journal.reserve(self.order, 7, 2)
        self.journal.mark_attempt(intent)
        self.journal.record_response(intent, 42, "PENDING")
        with self.assertRaises(RecoveryError):
            reconcile(self.trader, self.journal)
        self.assertIsNotNone(self.journal.open_proposal())

    def test_account_binding_rejects_changed_identity(self):
        self.journal.bind_identity("identity_one")
        self.journal.bind_identity("identity_one")
        with self.assertRaises(RecoveryError):
            self.journal.bind_identity("identity_two")

    def test_default_cli_does_not_enable_writes(self):
        with patch("trade_once.ledger_config", return_value={"LEDGER_BASE_URL": "https://example.test", "TRADER_API_KEY": "fake_key"}), patch("trade_once.LedgerClient", return_value=self.trader) as factory, patch("trade_once.MuseAdapter.from_config", return_value=Mock()), redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 0)
            self.assertFalse(factory.call_args.kwargs["allow_writes"])
            self.trader.post.assert_not_called()
            self.guardian.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
