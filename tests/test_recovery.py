import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from recovery import ProposalJournal, RecoveryError
from validation import ValidatedOrder


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "journal.sqlite3"
        self.journal = ProposalJournal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.order = ValidatedOrder("ETH", "BUY", Decimal("0.1"), "PRIVATE_PROSE",
                                    Decimal(320), Decimal("320.00"))
        self.record = {"transaction_id": 42, "symbol": "ETH", "action": "BUY",
                       "requested_quantity": 0.1, "theory_id": 7, "theory_version": 1,
                       "status": "EXECUTED", "executed_quantity": 0.1,
                       "executed_price": 3200, "executed_total": 320}

    def pending(self):
        intent = self.journal.reserve(self.order, 7, 1)
        self.journal.mark_attempt(intent)
        self.journal.record_response(intent, 42, "PENDING")
        return intent

    def test_lost_response_survives_restart_and_blocks_retry(self):
        intent = self.journal.reserve(self.order, 7, 1)
        self.journal.mark_attempt(intent)
        self.journal.close()
        self.journal = ProposalJournal(self.path)
        self.assertEqual(self.journal.open_proposal()["state"], "UNCERTAIN")
        with self.assertRaises(RecoveryError):
            self.journal.reserve(self.order, 7, 1)
        with self.assertRaises(RecoveryError):
            self.journal.mark_attempt(intent)
        with self.assertRaises(RecoveryError):
            self.journal.cancel_unsent(intent)
        with self.assertRaisesRegex(RecoveryError, "do not guess"):
            self.journal.reconcile_known(intent, self.record)

    def test_two_connections_cannot_reserve_two_open_proposals(self):
        second = ProposalJournal(self.path)
        try:
            self.journal.reserve(self.order, 7, 1)
            with self.assertRaises(RecoveryError):
                second.reserve(self.order, 7, 1)
        finally:
            second.close()

    def test_only_unsent_intent_can_be_cancelled(self):
        intent = self.journal.reserve(self.order, 7, 1)
        self.journal.cancel_unsent(intent)
        self.assertIsNone(self.journal.open_proposal())
        self.journal.reserve(self.order, 7, 1)

    def test_pending_and_escalated_remain_unresolved(self):
        intent = self.pending()
        with self.assertRaises(RecoveryError):
            self.journal.reserve(self.order, 7, 1)
        self.journal.reconcile_known(intent, self.record | {"status": "ESCALATED"})
        self.assertEqual(self.journal.open_proposal()["state"], "ESCALATED")
        with self.assertRaises(RecoveryError):
            self.journal.reserve(self.order, 7, 1)
        with self.assertRaises(RecoveryError):
            self.journal.reconcile_known(intent, self.record | {"status": "PENDING"})

    def test_terminal_fill_releases_gate_and_cannot_reopen(self):
        intent = self.pending()
        self.journal.reconcile_known(intent, self.record)
        self.assertIsNone(self.journal.open_proposal())
        self.journal.reconcile_known(intent, self.record)
        with self.assertRaises(RecoveryError):
            self.journal.reconcile_known(intent, self.record | {"status": "PENDING"})
        self.journal.reserve(self.order, 7, 1)
        self.assertEqual(self.journal.get(intent)["state"], "EXECUTED")

    def test_malformed_history_never_releases_gate(self):
        intent = self.pending()
        for changes in [{"transaction_id": 99}, {"theory_id": 8}, {"theory_version": True},
                        {"requested_quantity": 0.2}, {"symbol": "BTC"}, {"action": "SELL"},
                        {"status": "UNKNOWN"}, {"executed_total": None},
                        {"executed_quantity": 0.2}, {"executed_price": 0}]:
            with self.subTest(changes=changes), self.assertRaises(RecoveryError):
                self.journal.reconcile_known(intent, self.record | changes)
            self.assertEqual(self.journal.open_proposal()["state"], "PENDING")

    def test_proposal_ack_cannot_claim_execution_or_replace_id(self):
        intent = self.journal.reserve(self.order, 7, 1)
        with self.assertRaises(RecoveryError):
            self.journal.record_response(intent, 42, "PENDING")
        self.journal.mark_attempt(intent)
        with self.assertRaises(RecoveryError):
            self.journal.record_response(intent, 42, "EXECUTED")
        self.journal.record_response(intent, 42, "PENDING")
        with self.assertRaises(RecoveryError):
            self.journal.record_response(intent, 43, "PENDING")

    def test_rejection_and_block_are_distinct_terminal_outcomes(self):
        intent = self.pending()
        self.journal.reconcile_known(intent, self.record | {"status": "BLOCKED"})
        self.assertEqual(self.journal.get(intent)["state"], "BLOCKED")
        other = self.journal.reserve(self.order, 7, 1)
        self.journal.mark_attempt(other)
        self.journal.record_response(other, 43, "REJECTED")
        self.assertEqual(self.journal.get(other)["state"], "REJECTED")
        self.assertIsNone(self.journal.open_proposal())

    def test_journal_keeps_only_allowlisted_fields(self):
        intent = self.journal.reserve(self.order, 7, 1)
        self.assertNotIn("PRIVATE_PROSE", str(self.journal.get(intent)))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"PRIVATE_PROSE", self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
