"""Read and validate a submitted theory without proposing trades."""

import argparse
import math
import sys

from client import ApiError, Cooldown, LedgerClient
from configuration import ledger_config
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
        print(f"Theory check OK: id={theory.id}, version={theory.version}, symbol={args.symbol}")
        print(f"Strategy={theory.strategy}, lookback={theory.lookback_minutes} minutes")
        print(f"Threshold={theory.threshold_pct}%, position fraction={theory.position_fraction}")
        print(f"Stop loss={theory.stop_loss_pct}%, take profit={theory.take_profit_pct}%")
        print("Submitted theory and policy eligibility validated. No orders submitted.")
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
