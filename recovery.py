"""Small persistent JSON journal: save before sending, never guess after a failure."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from configuration import unique_object
from validation import SYMBOLS, number

OPEN_STATES = ("PLANNED", "UNCERTAIN", "PENDING", "ESCALATED")
SERVER_STATES = ("PENDING", "ESCALATED", "EXECUTED", "BLOCKED", "REJECTED")


class RecoveryError(RuntimeError):
    """Stop and reconcile before submitting another order."""


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise RecoveryError("Expected a positive integer identifier")
    return value


def number_from_text(value):
    """Decode our stored quantity, not model output."""
    return number(Decimal(value))


class ProposalJournal:
    """One JSON file per account. A lock prevents simultaneous runs (macOS/Linux)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Never silently leave an old database's unresolved orders behind.
        if self.path.suffix != ".json" or self.path.with_suffix(".sqlite3").exists():
            raise RecoveryError("Use a JSON journal; an existing SQLite journal requires deliberate migration")
        self.lock = self.path.with_suffix(".lock").open("a")
        self.path.with_suffix(".lock").chmod(0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RecoveryError("Another run is using this account journal") from None
        try:
            if self.path.exists():
                try:
                    self.data = json.loads(self.path.read_text(), object_pairs_hook=unique_object)
                except (ValueError, UnicodeError):
                    raise RecoveryError("Unreadable recovery file; investigate instead of resetting it") from None
                self._validate_file()
                self.path.chmod(0o600)
            else:
                self.data = {"version": 1, "identity": None, "proposals": []}
                self._save()
        except Exception:
            self.close()
            raise

    def close(self):
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()

    def _validate_file(self):
        """A damaged journal must never be treated as an empty journal."""
        try:
            if self.data["version"] != 1 or not isinstance(self.data["proposals"], list):
                raise ValueError
            if self.data["identity"] is not None and not isinstance(self.data["identity"], str):
                raise ValueError
            intents, transactions = set(), set()
            open_count = 0
            for row in self.data["proposals"]:
                if not isinstance(row["intent_id"], str) or not row["intent_id"] or row["intent_id"] in intents:
                    raise ValueError
                intents.add(row["intent_id"])
                if row["state"] not in (*OPEN_STATES, "EXECUTED", "BLOCKED", "REJECTED", "CANCELLED"):
                    raise ValueError
                if row["symbol"] not in SYMBOLS or row["action"] not in ("BUY", "SELL"):
                    raise ValueError
                if not isinstance(row["quantity"], str) or number_from_text(row["quantity"]) <= 0:
                    raise ValueError
                positive_id(row["theory_id"])
                positive_id(row["theory_version"])
                transaction_id = row["transaction_id"]
                if transaction_id is not None:
                    positive_id(transaction_id)
                    if transaction_id in transactions:
                        raise ValueError
                    transactions.add(transaction_id)
                if row["state"] in ("PENDING", "ESCALATED", "EXECUTED", "BLOCKED", "REJECTED") and transaction_id is None:
                    raise ValueError
                if row["review_decision"] not in (None, "APPROVE", "MODIFY", "BLOCK", "ESCALATE"):
                    raise ValueError
                open_count += row["state"] in OPEN_STATES
            if open_count > 1:
                raise ValueError
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise RecoveryError("Invalid recovery file; investigate it instead of deleting or resetting it") from None

    def _save(self):
        """Write a private temporary file, then atomically replace the old JSON."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(self.data, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _row(self, intent_id):
        for row in self.data["proposals"]:
            if row["intent_id"] == intent_id:
                return row
        raise RecoveryError("Unknown proposal intent")

    def _update(self, intent_id, **changes):
        self._row(intent_id).update(changes, updated_at=datetime.now(timezone.utc).isoformat())
        self._save()

    def get(self, intent_id):
        return deepcopy(self._row(intent_id))

    def open_proposal(self):
        for row in self.data["proposals"]:
            if row["state"] in OPEN_STATES:
                return deepcopy(row)
        return None

    def bind_identity(self, fingerprint):
        if self.data["identity"] not in (None, fingerprint):
            raise RecoveryError("Journal belongs to a different credential identity; do not bypass unresolved state")
        self.data["identity"] = fingerprint
        self._save()

    def reserve(self, order, theory_id, theory_version):
        if self.open_proposal() is not None:
            raise RecoveryError("An earlier proposal is unresolved; reconcile before proposing again")
        if order.symbol not in SYMBOLS or order.action not in ("BUY", "SELL"):
            raise RecoveryError("Only BUY/SELL proposals belong in the journal")
        quantity = number(order.quantity)
        if quantity <= 0:
            raise RecoveryError("Proposal quantity must be positive")
        positive_id(theory_id)
        positive_id(theory_version)
        intent_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self.data["proposals"].append({
            "intent_id": intent_id, "symbol": order.symbol, "action": order.action,
            "quantity": format(quantity, "f"), "theory_id": theory_id,
            "theory_version": theory_version, "state": "PLANNED", "transaction_id": None,
            "review_decision": None, "created_at": now, "updated_at": now,
        })
        self._save()
        return intent_id

    def mark_attempt(self, intent_id):
        if self.get(intent_id)["state"] != "PLANNED":
            raise RecoveryError("Proposal cannot be sent or retried in its current state")
        self._update(intent_id, state="UNCERTAIN")

    def cancel_unsent(self, intent_id):
        if self.get(intent_id)["state"] != "PLANNED":
            raise RecoveryError("An attempted proposal requires Ledger reconciliation")
        self._update(intent_id, state="CANCELLED")

    def review_attempted(self, intent_id):
        return self.get(intent_id)["review_decision"] is not None

    def mark_review_attempt(self, intent_id, decision):
        if decision not in ("APPROVE", "MODIFY", "BLOCK", "ESCALATE"):
            raise RecoveryError("Invalid review decision")
        if self.get(intent_id)["state"] != "PENDING" or self.review_attempted(intent_id):
            raise RecoveryError("Review cannot be sent or retried; reconcile first")
        self._update(intent_id, review_decision=decision)

    def record_response(self, intent_id, transaction_id, status):
        positive_id(transaction_id)
        if status not in ("PENDING", "REJECTED"):
            raise RecoveryError("Unexpected proposal status; leave the attempt unresolved")
        row = self.get(intent_id)
        if row["state"] != "UNCERTAIN" or row["transaction_id"] is not None:
            raise RecoveryError("Cannot replace a recorded proposal response")
        if any(saved["transaction_id"] == transaction_id for saved in self.data["proposals"]):
            raise RecoveryError("Transaction identifier is already recorded")
        self._update(intent_id, transaction_id=transaction_id, state=status)

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

        if current["state"] not in ("PENDING", "ESCALATED"):
            if current["state"] != status:
                raise RecoveryError("Cannot change a completed proposal's outcome")
            return
        self._update(intent_id, state=status)
