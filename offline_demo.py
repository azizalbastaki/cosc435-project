"""Trader -> simulated Ledger queue -> independent Guardian. Never live trading."""

import argparse
from copy import deepcopy

from guardian import review_next
from model_adapter import MuseAdapter
from trader import decide

FIXTURE = {
    "policy": {"allowed_symbols": ["ETH"], "restricted_actions": ["WITHDRAW"],
               "max_transaction_amount": 1000, "max_trades_per_hour": 10,
               "max_trades_per_day": 50, "trades_last_hour": 0, "trades_last_day": 0,
               "escalation_threshold": 750},
    "portfolio": {"cash_balance": 10000, "holdings": [{"symbol": "ETH", "quantity": 0.2}]},
    "quote": {"symbol": "ETH", "price": 3200},
    "strategy": {"selected_symbols": ["ETH"], "position_fraction": 0.05},
}


class FixtureSource:
    """Role-local copies of evidence; only the simulated queue is shared."""
    def __init__(self, queue, fixture=None):
        self.queue = queue
        self.data = deepcopy(FIXTURE if fixture is None else fixture)
        self.reads = []

    def _read(self, name):
        self.reads.append(name)
        return deepcopy(self.data[name])

    def policy(self):
        return self._read("policy")

    def portfolio(self):
        return self._read("portfolio")

    def quote(self, symbol):
        quote = self._read("quote")
        if quote["symbol"] != symbol:
            raise ValueError("Fixture has no quote for selected symbol")
        return quote

    def strategy(self):
        return self._read("strategy")

    def pending(self):
        self.reads.append("pending")
        return deepcopy(self.queue[0]) if self.queue else None


class ScriptedModel:
    """Explicit test double, not an LLM or a replacement for course LLM use."""
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def generate(self, system_prompt, payload, schema):
        self.calls += 1
        return deepcopy(self.result)


def run_demo(trader_model, guardian_model, scenario="normal"):
    queue = []
    trader_source = FixtureSource(queue)
    guardian_source = FixtureSource(queue)
    if scenario == "normal":
        candidate = decide(trader_source, trader_model, "ETH")
        print(f"Trader: {candidate.action}")
        if candidate.action != "HOLD":
            # In-memory transport only. No Ledger client is imported or invoked.
            queue.append({"transaction_id": 1, "symbol": candidate.symbol,
                          "action": candidate.action, "requested_quantity": candidate.quantity,
                          "reasoning": candidate.reasoning})
    else:
        # Deliberately bypass the trader to emulate a compromised external proposer.
        queue.append({"transaction_id": 1, "symbol": "ETH", "action": "BUY" if scenario == "oversized" else "SELL",
                      "requested_quantity": 10 if scenario == "oversized" else 0.3,
                      "reasoning": "Untrusted injected test order."})
        print("Injected a synthetic pending order; trader validation deliberately bypassed.")
    result = review_next(guardian_source, guardian_model)
    print(f"Guardian: {result['decision']} — {result['reason']}")
    print("Guardian reads: " + ", ".join(guardian_source.reads))
    print("Simulation only: no Ledger requests, fills, proposals, or reviews.")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("normal", "oversized", "unsupported-sale"), default="normal")
    parser.add_argument("--live-model", action="store_true", help="Use billable Muse calls with synthetic evidence; Ledger remains offline")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    adapter = None
    try:
        if args.live_model:
            adapter = MuseAdapter.from_config(args.config)
            trader_model = guardian_model = adapter
            print("Live Muse models; synthetic Ledger evidence only.")
        else:
            print("Offline demo with scripted model responses (not live LLM decisions).")
            trader_model = ScriptedModel({"symbol": "ETH", "action": "BUY", "quantity": 0.1,
                                          "reasoning": "Synthetic demonstration candidate."})
            guardian_model = ScriptedModel({"decision": "APPROVE", "reason": "Synthetic review.",
                                            "risk_score": 5, "modified_quantity": None, "rules_triggered": []})
        run_demo(trader_model, guardian_model, args.scenario)
        return 0
    except ValueError:
        print("Invalid model configuration; check the local Muse settings.")
        return 1
    finally:
        if adapter is not None:
            adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
