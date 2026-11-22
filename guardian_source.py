"""Read-only Guardian evidence collection. No model calls or Ledger reviews."""

from dataclasses import dataclass, field
from decimal import Decimal

from smoke_test import SYMBOLS, summary
from validation import number


@dataclass(frozen=True)
class PendingEvidence:
    transaction_id: int
    symbol: str
    action: str
    quantity: Decimal
    portfolio: dict = field(repr=False)
    policy: dict = field(repr=False)
    market: dict = field(repr=False)


class GuardianSource:
    def __init__(self, api):
        if api.role != "guardian":
            raise ValueError("Guardian evidence requires a guardian client")
        self.api = api

    def collect(self, limit=2):
        """One small batch; never drain has_more or trust pending-order context.

        This is observational only. The evidence is not sufficient to authorize
        execution without theory provenance, model review, and a final recheck.
        """
        if type(limit) is not int or not 1 <= limit <= 2:
            raise ValueError("Use a batch size of one or two")
        pending = self.api.get("trade_pending.php", limit=limit)
        try:
            rows, more = pending["pending"], pending["has_more"]
            if not isinstance(rows, list) or len(rows) > limit or type(more) is not bool:
                raise ValueError
            parsed = []
            seen = set()
            for row in rows:
                transaction_id = row["transaction_id"]
                symbol, action = row["symbol"], row["action"]
                if type(transaction_id) is not int or transaction_id <= 0 or transaction_id in seen:
                    raise ValueError
                if symbol not in SYMBOLS or action not in ("BUY", "SELL"):
                    raise ValueError
                quantity = number(row["requested_quantity"])
                if quantity <= 0:
                    raise ValueError
                seen.add(transaction_id)
                parsed.append((transaction_id, symbol, action, quantity))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid pending-order response; no review attempted") from None
        snapshots = []
        for transaction_id, symbol, action, quantity in parsed:
            # Fetch all evidence directly, per order; ignore the queue's context,
            # requested_price, estimated_total, and any trader validation claims.
            policy = self.api.get("policies.php")
            portfolio = self.api.get("portfolio.php")
            market = self.api.get("market_prices.php", symbols=symbol)
            summary(portfolio, policy, market, symbol)  # Validate displayed fields.
            snapshots.append(PendingEvidence(transaction_id, symbol, action, quantity,
                                             portfolio, policy, market))
        return snapshots, more
