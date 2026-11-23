"""Offline lost-response/restart demonstration using a temporary journal."""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from recovery import ProposalJournal, RecoveryError
from validation import ValidatedOrder


def main():
    # Synthetic order only; no credentials, Ledger client, or model is used.
    order = ValidatedOrder("ETH", "BUY", Decimal("0.1"), "Test only",
                           Decimal("320"), Decimal("320.00"))
    with TemporaryDirectory() as folder:
        path = Path(folder) / "journal.sqlite3"
        journal = ProposalJournal(path)
        intent = journal.reserve(order, theory_id=7, theory_version=1)
        journal.mark_attempt(intent)
        journal.close()
        print("Simulated lost response: attempt saved as UNCERTAIN.")

        journal = ProposalJournal(path)
        try:
            print("After restart: " + journal.open_proposal()["state"])
            try:
                journal.reserve(order, theory_id=7, theory_version=1)
            except RecoveryError:
                print("Duplicate proposal prevented. Investigate Ledger before any new proposal.")
        finally:
            journal.close()
    print("Offline demonstration finished. No network calls or orders sent.")


if __name__ == "__main__":
    main()
