"""Durable local proposal journal. No network calls or automatic retries.

Future submission order: reserve -> mark_attempt -> send exactly once -> record
response. Marking the attempt BEFORE sending makes a crash fail closed.
"""

import sqlite3
from pathlib import Path
from uuid import uuid4

from validation import SYMBOLS, number

OPEN_STATES = ("PLANNED", "UNCERTAIN", "PENDING", "ESCALATED")
SERVER_STATES = ("PENDING", "ESCALATED", "EXECUTED", "BLOCKED", "REJECTED")


class RecoveryError(RuntimeError):
    """Unresolved or inconsistent state: do not submit another proposal."""


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise RecoveryError("Expected a positive integer identifier")
    return value


class ProposalJournal:
    """Use one private database per Ledger account, shared by its trader runs.

    This coordinates only processes using that file. It cannot see proposals
    from other devices or clients; a future live cycle must also read Ledger.
    """

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        # SQLite commits make the intent/attempt durable before returning.
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                intent_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity TEXT NOT NULL,
                theory_id INTEGER NOT NULL,
                theory_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                transaction_id INTEGER UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Database-enforced: two processes cannot reserve two open proposals.
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_proposal
            ON proposals ((1))
            WHERE state IN ('PLANNED', 'UNCERTAIN', 'PENDING', 'ESCALATED')
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def open_proposal(self):
        row = self.db.execute("""
            SELECT * FROM proposals
            WHERE state IN ('PLANNED', 'UNCERTAIN', 'PENDING', 'ESCALATED')
        """).fetchone()
        return dict(row) if row else None

    def get(self, intent_id):
        row = self.db.execute("SELECT * FROM proposals WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is None:
            raise RecoveryError("Unknown proposal intent")
        return dict(row)

    def reserve(self, order, theory_id, theory_version):
        """Save a locally validated BUY/SELL before any attempt; never store keys/prose."""
        if order.symbol not in SYMBOLS or order.action not in ("BUY", "SELL"):
            raise RecoveryError("Only BUY/SELL proposals belong in the journal")
        quantity = number(order.quantity)
        if quantity <= 0:
            raise RecoveryError("Proposal quantity must be positive")
        positive_id(theory_id)
        positive_id(theory_version)
        intent_id = uuid4().hex
        try:
            with self.db:
                self.db.execute("""
                    INSERT INTO proposals
                    (intent_id, symbol, action, quantity, theory_id, theory_version, state)
                    VALUES (?, ?, ?, ?, ?, ?, 'PLANNED')
                """, (intent_id, order.symbol, order.action, format(quantity, "f"),
                      theory_id, theory_version))
        except sqlite3.IntegrityError:
            raise RecoveryError("An earlier proposal is unresolved; reconcile before proposing again") from None
        return intent_id

    def mark_attempt(self, intent_id):
        """Commit UNCERTAIN immediately BEFORE the future HTTP write."""
        with self.db:
            cursor = self.db.execute("""
                UPDATE proposals SET state = 'UNCERTAIN', updated_at = CURRENT_TIMESTAMP
                WHERE intent_id = ? AND state = 'PLANNED'
            """, (intent_id,))
            if cursor.rowcount != 1:
                raise RecoveryError("Proposal cannot be sent or retried in its current state")

    def cancel_unsent(self, intent_id):
        """Only a reserved intent that was never marked for sending can be cancelled."""
        with self.db:
            cursor = self.db.execute("""
                UPDATE proposals SET state = 'CANCELLED', updated_at = CURRENT_TIMESTAMP
                WHERE intent_id = ? AND state = 'PLANNED'
            """, (intent_id,))
            if cursor.rowcount != 1:
                raise RecoveryError("An attempted proposal requires Ledger reconciliation")

    def record_response(self, intent_id, transaction_id, status):
        """Record an initial BUY/SELL proposal reply, not a presumed execution."""
        positive_id(transaction_id)
        if status not in ("PENDING", "REJECTED"):
            raise RecoveryError("Unexpected proposal status; leave the attempt unresolved")
        try:
            with self.db:
                cursor = self.db.execute("""
                    UPDATE proposals SET transaction_id = ?, state = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE intent_id = ? AND state = 'UNCERTAIN' AND transaction_id IS NULL
                """, (transaction_id, status, intent_id))
                if cursor.rowcount != 1:
                    raise RecoveryError("Cannot replace a recorded proposal response")
        except sqlite3.IntegrityError:
            raise RecoveryError("Transaction identifier is already recorded") from None

    def reconcile_known(self, intent_id, transaction):
        """Apply a validated Ledger history record ONLY for an already known ID.

        Unknown-ID uncertain attempts need deliberate investigation; matching a
        similar-looking history row does not prove which request produced it.
        """
        current = self.get(intent_id)
        if current["transaction_id"] is None:
            raise RecoveryError("Lost transaction ID: investigate in Ledger; do not guess or resend")
        try:
            transaction_id = positive_id(transaction["transaction_id"])
            if transaction_id != current["transaction_id"]:
                raise RecoveryError("History transaction ID does not match")
            positive_id(transaction["theory_id"])
            positive_id(transaction["theory_version"])
            for name in ("symbol", "action", "theory_id", "theory_version"):
                if transaction[name] != current[name]:
                    raise RecoveryError("History record does not match the saved intent")
            if number(transaction["requested_quantity"]) != number_from_text(current["quantity"]):
                raise RecoveryError("History quantity does not match the saved intent")
            status = transaction["status"]
            if status not in SERVER_STATES:
                raise RecoveryError("Unknown history status")
            if current["state"] == "ESCALATED" and status == "PENDING":
                raise RecoveryError("History cannot move an escalated order back to pending")
            if status == "EXECUTED":
                for name in ("executed_quantity", "executed_price", "executed_total"):
                    if number(transaction[name]) <= 0:
                        raise RecoveryError("Executed status needs a positive recorded fill")
                if number(transaction["executed_quantity"]) > number_from_text(current["quantity"]):
                    raise RecoveryError("Recorded fill exceeds requested quantity")
        except (KeyError, TypeError, ValueError):
            raise RecoveryError("Incomplete or invalid history record") from None

        with self.db:
            cursor = self.db.execute("""
                UPDATE proposals SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE intent_id = ? AND state IN ('PENDING', 'ESCALATED')
            """, (status, intent_id))
            if cursor.rowcount != 1:
                # An identical terminal observation is harmless; do not reopen it.
                if self.get(intent_id)["state"] != status:
                    raise RecoveryError("Cannot change a completed proposal's outcome")


def number_from_text(value):
    """Only for our own stored Decimal representation, not untrusted model output."""
    from decimal import Decimal
    return number(Decimal(value))
