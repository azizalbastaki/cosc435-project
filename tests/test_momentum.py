"""Offline momentum coverage: pure boundaries, replay, and independent Guardian evidence."""
import copy
import tempfile
import unittest
import json
import shutil
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

from guardian import rules
from trading_agent import strategy, theory_check, model_decision, replay
from test_system import MODEL, THEORY, U, agent_cfg, guardian_cfg, StubTrader, StubGuardian, Quiet
from fake_ledger import FakeLedger, fmt
from common import market_model as mm
from trading_agent.agent import TradingAgent
from guardian.guardian import Guardian


TH = dict(THEORY, strategy="momentum", lookback_minutes=10, threshold_pct=0.5,
          position_fraction=0.05, stop_loss_pct=5, take_profit_pct=10)
LIMITS = {"max_order_usd": 9000, "min_order_usd": 100}


class MomentumTests(unittest.TestCase):
    def signal(self, price, held=0, avg=0, start=100, cash=100000):
        return strategy.decide(price, None, held, avg, cash, TH, LIMITS, start_price=start)

    def test_threshold_boundaries_and_no_short_sales(self):
        for price, held, action in [(100.5, 0, "BUY"), (100.499999, 0, "HOLD"),
                                    (99.5, 1, "SELL"), (99.500001, 1, "HOLD"),
                                    (99.5, 0, "HOLD"), (100, 0, "HOLD")]:
            with self.subTest(price=price, held=held):
                result = self.signal(price, held, 100)
                self.assertEqual(result.action, action)
                self.assertAlmostEqual(result.features["lookback_return_pct"], (price / 100 - 1) * 100)
                self.assertNotIn("deviation_pct", result.features)
        self.assertEqual(self.signal(101, cash=1).action, "HOLD")

    def test_risk_exits_override_opposing_signals(self):
        # Positive momentum cannot buy through a stop loss or take profit.
        self.assertEqual(self.signal(95, 1, 100, start=90).kind, "stop_loss")
        self.assertEqual(self.signal(110, 1, 100, start=100).kind, "take_profit")

    def test_theory_pin_and_mean_reversion_constraints_are_separate(self):
        cfg = {"symbol": "SOL", "expected_theory": {"version": TH["version"],
               "strategy": "momentum", "params": {"lookback_minutes": 10, "threshold_pct": 0.5}}}
        self.assertEqual(theory_check.validate_theory(TH, cfg)["strategy"], "momentum")
        for changed in (dict(TH, strategy="custom"), dict(TH, threshold_pct=0.6),
                        dict(TH, position_fraction=0.9), dict(TH, version=99)):
            with self.assertRaises(theory_check.TheoryInvalid):
                theory_check.validate_theory(changed, cfg)
        with self.assertRaises(theory_check.TheoryInvalid):
            theory_check.validate_theory(dict(TH, strategy="mean_reversion"),
                                         {"symbol": "SOL", "expected_theory": {}})

    def context(self, price, action, held=0, avg=100):
        now = U("2026-11-22T12:00:00")
        order = {"action": action, "symbol": "SOL", "requested_price": price,
                 "requested_quantity": 0.5, "theory_id": TH["id"], "theory_version": TH["version"],
                 "created_at": fmt(now), "transaction_id": 1, "trader_key_label": "sol-trader"}
        history = [{"transaction_id": 0, "symbol": "SOL", "status": "EXECUTED", "action": "BUY",
                    "executed_quantity": held, "executed_price": avg}]
        return rules.Context(order, {"allowed_symbols": ["SOL"]}, {},
                             {"cash_balance": 100000, "holdings": [{"symbol": "SOL", "quantity": held}]},
                             history, TH, MODEL, [], [], guardian_cfg("/tmp"), now, quote=price)

    def test_guardian_independently_agrees_and_rejects_opposite_direction(self):
        for price, held, avg, start in [(100.5, 0, 100, 100), (99.5, 1, 100, 100),
                                       (95, 1, 100, 90), (110, 1, 100, 100)]:
            sig = self.signal(price, held, avg, start)
            c = self.context(price, sig.action, held, avg)
            def quote(model, symbol, timestamp):
                return start if timestamp == c.now - 600 else price
            with patch("guardian.rules.mm.quote", side_effect=quote):
                self.assertEqual(rules.theory_checks(c), [])
                c.order["action"] = "SELL" if sig.action == "BUY" else "BUY"
                self.assertTrue(any(f.code.startswith("OFF_THEORY") for f in rules.theory_checks(c)))
        c = self.context(99, "SELL")
        self.assertIn("OVERSELL", [f.code for f in rules.hard_rules(c)])
        c.theory = dict(TH, version=999)
        self.assertIn("THEORY_INVALID", [f.code for f in rules.theory_checks(c)])

    def test_model_payload_and_output_gate_are_strategy_specific(self):
        sig = self.signal(101)
        bounds = {"max_buy_quantity": Decimal(1), "max_sell_quantity": Decimal(0)}
        payload = model_decision.build_payload("SOL", {"price": 101, "timestamp_unix": 1000}, sig,
                  bounds, {"cash": 1000, "held": 0, "avg_cost": 0}, {}, TH, [])
        self.assertEqual(payload["signals"]["lookback_return_pct"], 1)
        self.assertNotIn("mean_24h", payload["signals"])
        self.assertNotIn("deviation_pct", payload["signals"])
        for action, qty in [("SELL", 1), ("BUY", 2), ("BUY", True)]:
            with self.assertRaises(model_decision.Invalid):
                model_decision.validate({"symbol": "SOL", "action": action, "quantity": qty,
                                         "reasoning": "Test"}, "SOL", sig, bounds)

    def test_replay_passes_exact_start_not_first_minute_sample(self):
        start = int(U("2026-11-22T12:00:00"))
        calls = []
        original = strategy.decide
        def record(*args, **kwargs):
            calls.append(kwargs["start_price"])
            return original(*args, **kwargs)
        with patch("trading_agent.replay.strategy.decide", side_effect=record):
            replay.replay(MODEL, TH, start, start + 600)
        self.assertEqual(calls, [mm.quote(MODEL, "SOL", start - 600), mm.quote(MODEL, "SOL", start - 300)])

    def offline_cycle(self, tighten_policy=False):
        start = int(U("2026-11-22T12:00:00"))
        now = next(t for t in range(start, start + 86400, 60)
                   if mm.quote(MODEL, "SOL", t) / mm.quote(MODEL, "SOL", t - 600) >= 1.006)
        ledger = FakeLedger(now)
        ledger.theory_obj = copy.deepcopy(TH)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent_cfg(tmp)
            cfg["expected_theory"] = {"version": TH["version"], "strategy": "momentum", "params": {}}
            agent = TradingAgent(cfg, client=ledger, clock=lambda: ledger.now, log=Quiet())
            agent.llm = StubTrader()
            agent.cycle()
            self.assertEqual(ledger.txs[-1]["action"], "BUY")
            self.assertEqual(agent.llm.seen[-1]["theory_settings"]["strategy"], "momentum")
            reviewer = Guardian(guardian_cfg(tmp), client=ledger, clock=lambda: ledger.now, log=Quiet())
            reviewer.llm = StubGuardian()
            if tighten_policy:
                original = reviewer.llm.ask_json
                def tighten(*args, **kwargs):
                    result = original(*args, **kwargs)
                    ledger.policy_obj["restricted_actions"] = ["WITHDRAW", "BUY"]
                    return result
                reviewer.llm.ask_json = tighten
            reviewer.run_once()
            self.assertEqual(ledger.txs[-1]["status"], "BLOCKED" if tighten_policy else "EXECUTED")

    def test_active_ledger_theory_drives_full_offline_cycle(self):
        self.offline_cycle()

    def test_momentum_final_recheck_blocks_new_policy_restriction(self):
        self.offline_cycle(tighten_policy=True)

    def test_momentum_configuration_does_not_inherit_mean_reversion_presets(self):
        from tools import configure
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for role in ("trading_agent", "guardian"):
                (target / role).mkdir()
                shutil.copy(root / role / "config.example.json", target / role / "config.example.json")
            with patch.object(configure, "ROOT", target):
                with self.assertRaises(ValueError):
                    configure.write_configs("ETH", 2, "eth-trader", strategy="momentum")
                self.assertFalse((target / "trading_agent/config.json").exists())
                configure.write_configs("ETH", 2, "eth-trader", strategy="momentum", pin=False)
            cfg = json.loads((target / "trading_agent/config.json").read_text())
            self.assertEqual(cfg["expected_theory"], dict(cfg["expected_theory"],
                             strategy="momentum", version=2, params={}))
            guardian = json.loads((target / "guardian/config.json").read_text())
            self.assertEqual(guardian["allowed_symbols"], ["ETH"])

    def test_replay_rejects_insufficient_history_and_invalid_theory(self):
        with self.assertRaises(ValueError):
            replay.replay(MODEL, TH, MODEL["epoch_unix"], MODEL["epoch_unix"] + 600)
        start = int(U("2026-11-22T12:00:00"))
        with self.assertRaises(theory_check.TheoryInvalid):
            replay.replay(MODEL, dict(TH, strategy="custom"), start, start + 600)
