"""Guardian-only live source for dry-run decisions; all operations are GETs."""

from dataclasses import asdict

from market import MarketReader
from theory import positive_integer, validate_theory
from validation import SYMBOLS, ValidationError, number


class LiveGuardianSource:
    def __init__(self, api):
        if api.role != "guardian":
            raise ValueError("A guardian-scoped client is required")
        self.api = api
        self.market = MarketReader(api)
        self.order = None
        self.theory = None
        self.signals = None
        self.holdings = []

    def pending(self):
        reply = self.api.get("trade_pending.php", limit=1)
        rows = reply["pending"]
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValidationError("Expected at most one pending order")
        if not rows:
            return None
        row = rows[0]
        # Do not retain queue context, estimated amounts, or trader labels.
        order = {name: row[name] for name in (
            "transaction_id", "theory_id", "theory_version", "symbol", "action",
            "requested_quantity", "reasoning")}
        for name in ("transaction_id", "theory_id", "theory_version"):
            positive_integer(order[name], name)
        if order["symbol"] not in SYMBOLS:
            raise ValidationError("Unsupported pending currency")
        self.order = order
        return order

    def policy(self):
        # Each initial/final evidence sweep must recheck theory provenance too.
        self.theory = None
        return self.api.get("policies.php")

    def portfolio(self):
        portfolio = self.api.get("portfolio.php")
        self.holdings = []
        for holding in portfolio["holdings"]:
            self.holdings.append({
                "symbol": holding["symbol"], "quantity": number(holding["quantity"]),
                "avg_cost": number(holding["avg_cost"]),
            })
        return portfolio

    def matching_theory(self):
        if self.theory is None:
            theory = validate_theory(self.api.get("theory.php"))
            if (theory.id != self.order["theory_id"]
                    or theory.version != self.order["theory_version"]):
                raise ValidationError("Pending order theory version is unavailable")
            if self.order["symbol"] not in theory.selected_symbols:
                raise ValidationError("Pending symbol is not selected by its theory")
            if theory.strategy == "custom":
                raise ValidationError("Custom strategy review needs explicit rule implementation")
            self.theory = theory
        return self.theory

    def quote(self, symbol):
        theory = self.matching_theory()
        self.signals = self.market.read(symbol, theory.lookback_minutes)
        return {"symbol": symbol, "price": self.signals.current_price}

    def strategy(self):
        theory = self.matching_theory()
        return {"selected_symbols": list(theory.selected_symbols),
                "position_fraction": theory.position_fraction}

    def supporting_evidence(self):
        return {"transaction_id": self.order["transaction_id"],
                "submitted_theory": asdict(self.theory), "signals": asdict(self.signals),
                "holdings": self.holdings}
