"""Read-only theory-based market replay check. Never produces an order."""

import argparse
import math
import sys

from client import ApiError, Cooldown, LedgerClient
from configuration import ledger_config
from market import MarketReader
from theory import read_theory
from validation import SYMBOLS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--symbol", type=str.upper, choices=sorted(SYMBOLS), default="ETH")
    args = parser.parse_args(argv)
    api = None
    try:
        config = ledger_config(args.config)
        api = LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"])
        theory = read_theory(api, args.symbol)
        signals = MarketReader(api).read(args.symbol, theory.lookback_minutes)
        print(f"Replay check OK: {args.symbol}, theory {theory.id}/v{theory.version}, {signals.sample_count} samples")
        print(f"Quote ${signals.current_price:.8f} | replay ${signals.replayed_price:.8f}")
        print(f"Lookback return {signals.lookback_return_pct:.6f}% | mean deviation {signals.mean_deviation_pct:.6f}%")
        signal = signals.strategy_signal(theory.strategy)
        if signal is None:
            print("Custom strategy: no automatic signal interpretation implemented.")
        else:
            print(f"{theory.strategy} signal {signal:.6f}% | theory threshold {theory.threshold_pct}%")
        print("Evidence only; no trade decision, model call, or order submitted.")
        return 0
    except Cooldown as exc:
        print(f"HOLD: Ledger cooldown; wait at least {math.ceil(exc.seconds)} seconds.", file=sys.stderr)
        return 2
    except (ValueError, ApiError) as exc:
        print(f"HOLD: {exc}", file=sys.stderr)
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    sys.exit(main())
