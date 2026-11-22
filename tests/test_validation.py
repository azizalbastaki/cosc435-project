import copy
import unittest
from decimal import Decimal, localcontext

from validation import (ValidationError, bounded_quantity, cash_amount,
                        normalize_list, policy_amount, validate_order)


class OrderValidationTests(unittest.TestCase):
    def setUp(self):
        self.order = dict(symbol="ETH", action="BUY", quantity=1, reasoning="Entry signal met.")
        self.context = dict(
            policy=dict(allowed_symbols=["ETH", "SOL"], restricted_actions=["WITHDRAW"],
                        max_transaction_amount=10000, max_trades_per_hour=10,
                        max_trades_per_day=50, trades_last_hour=0, trades_last_day=0),
            portfolio=dict(cash_balance=8000, holdings=[dict(symbol="ETH", quantity=2)]),
            quote=dict(symbol="ETH", price=250), selected_symbols=["ETH"], position_fraction=0.05,
        )

    def check(self):
        return validate_order(self.order, **self.context)

    def test_buy_and_sell_at_exact_boundaries(self):
        self.context["policy"]["max_transaction_amount"] = 250
        result = self.check()
        self.assertEqual(result.settled_amount, Decimal("250.00"))
        self.assertEqual(result.quantity, Decimal(1))
        self.order.update(action="SELL", quantity=2)
        self.context["policy"]["max_transaction_amount"] = 500
        self.assertEqual(self.check().settled_amount, Decimal("500.00"))

    def test_guide_rounding_fixture(self):
        quantity = bounded_quantity(1000, 60600)
        self.assertEqual(quantity, Decimal("0.0165016501"))
        self.assertEqual(cash_amount(quantity, 60600, "BUY"), Decimal("1000.00"))
        self.assertEqual(cash_amount(quantity, 60600, "SELL"), Decimal("999.99"))
        self.assertEqual(bounded_quantity(250, 200), Decimal("1.2500000000"))

    def test_exact_and_rounded_amounts_cannot_bypass_ceiling(self):
        self.context["quote"]["price"] = Decimal("250.001")
        for action, cap in [("BUY", Decimal("250.005")), ("SELL", Decimal("250.00"))]:
            with self.subTest(action=action):
                self.order["action"] = action
                self.context["policy"]["max_transaction_amount"] = cap
                with self.assertRaisesRegex(ValidationError, "ceiling"):
                    self.check()
        self.assertEqual(policy_amount(1, Decimal("250.001"), "SELL"), Decimal("250.001"))
        self.assertEqual(policy_amount(1, Decimal("250.001"), "BUY"), Decimal("250.01"))

    def test_quantity_type_range_and_precision(self):
        for value in [True, False, "1", None, float("nan"), float("inf"), -1, 0,
                      Decimal("0.00000000001"), 10**18]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.order["quantity"] = value
                self.check()

    def test_hold_is_local_and_ignores_trading_context(self):
        result = validate_order(dict(symbol="", action="HOLD", quantity=0, reasoning=" Wait. "),
                                policy=None, portfolio=None, quote=None,
                                selected_symbols=None, position_fraction=None)
        self.assertEqual(result.reasoning, "Wait.")
        self.assertEqual(result.settled_amount, 0)
        self.order.update(action="HOLD", quantity=1)
        with self.assertRaises(ValidationError):
            self.check()

    def test_exact_fields_action_and_reason(self):
        for change in [dict(extra=True), dict(action="WITHDRAW"), dict(reasoning="  "),
                       dict(reasoning="x\x00y"), dict(reasoning="é" * 8001),
                       dict(reasoning="\ud800"), dict(symbol=[]), dict(action={})]:
            with self.subTest(change=list(change)):
                original = self.order.copy()
                self.order.update(change)
                with self.assertRaises(ValidationError):
                    self.check()
                self.order = original
        self.order["reasoning"] = "é" * 8000
        self.check()
        del self.order["quantity"]
        with self.assertRaises(ValidationError):
            self.check()

    def test_symbol_policy_and_strategy_intersection(self):
        for symbols in [[], ["SOL"]]:
            self.context["policy"]["allowed_symbols"] = symbols
            with self.assertRaises(ValidationError):
                self.check()
        self.context["policy"]["allowed_symbols"] = ["ETH"]
        self.context["selected_symbols"] = ["SOL"]
        with self.assertRaises(ValidationError):
            self.check()

    def test_policy_lists_and_restricted_actions(self):
        self.assertEqual(normalize_list(" eth, sol "), {"ETH", "SOL"})
        self.context["policy"]["allowed_symbols"] = "ETH,SOL"
        self.check()
        for action in ("BUY", "SELL"):
            self.order["action"] = action
            self.context["policy"]["restricted_actions"] = "WITHDRAW," + action
            with self.assertRaisesRegex(ValidationError, "restricted"):
                self.check()
        for invalid in [None, [True], [""], "ETH,,SOL"]:
            with self.assertRaises(ValidationError):
                normalize_list(invalid)

    def test_quotas_at_and_above_limit(self):
        for field, limit in [("trades_last_hour", 10), ("trades_last_day", 50)]:
            for count in (limit, limit + 1):
                self.context["policy"][field] = count
                with self.assertRaisesRegex(ValidationError, "quota"):
                    self.check()
            self.context["policy"][field] = limit - 1
            self.check()
            self.context["policy"][field] = 0

    def test_cash_fraction_and_rounding(self):
        self.context["portfolio"]["cash_balance"] = 249
        with self.assertRaisesRegex(ValidationError, "Insufficient cash"):
            self.check()
        self.context["portfolio"]["cash_balance"] = 8000
        self.context["quote"]["price"] = 400
        self.check()
        self.context["quote"]["price"] = Decimal("400.00000001")
        with self.assertRaisesRegex(ValidationError, "fraction"):
            self.check()
        for fraction in (True, "0.05", 0, 1.1, Decimal("0.05001")):
            self.context["position_fraction"] = fraction
            with self.assertRaises(ValidationError):
                self.check()

    def test_sell_requires_holdings(self):
        self.order.update(action="SELL", quantity=Decimal("2.0000000001"))
        with self.assertRaisesRegex(ValidationError, "holdings"):
            self.check()
        self.order["quantity"] = 1
        self.context["portfolio"]["holdings"] = []
        with self.assertRaisesRegex(ValidationError, "holdings"):
            self.check()
        self.context["portfolio"]["holdings"] = [dict(symbol="ETH", quantity=2)] * 2
        with self.assertRaisesRegex(ValidationError, "Duplicate"):
            self.check()

    def test_subcent_sell_and_rounded_buy(self):
        self.context["quote"]["price"] = Decimal("0.001")
        self.assertEqual(self.check().settled_amount, Decimal("0.01"))
        self.order["action"] = "SELL"
        with self.assertRaisesRegex(ValidationError, "one cent"):
            self.check()

    def test_invalid_context_and_quote(self):
        original = copy.deepcopy(self.context)
        for section, field, value in [("quote", "price", 0), ("quote", "price", "250"),
                                      ("quote", "symbol", "SOL"),
                                      ("policy", "trades_last_hour", True),
                                      ("policy", "max_transaction_amount", -1)]:
            self.context = copy.deepcopy(original)
            self.context[section][field] = value
            with self.assertRaises(ValidationError):
                self.check()
        self.context = original
        del self.context["quote"]["price"]
        with self.assertRaises(ValidationError):
            self.check()

    def test_input_not_mutated_and_decimal_context_independent(self):
        original = copy.deepcopy((self.order, self.context))
        with localcontext() as context:
            context.prec = 4
            self.assertEqual(self.check().settled_amount, Decimal("250.00"))
            self.assertEqual(cash_amount(Decimal("999999999999999999.9999999999"), 1, "BUY"),
                             Decimal("1000000000000000000.00"))
        self.assertEqual((self.order, self.context), original)


if __name__ == "__main__":
    unittest.main()
