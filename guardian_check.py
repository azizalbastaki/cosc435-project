"""Inspect a small pending-order batch using a separate Guardian Ledger key."""

import argparse
import math
import sys

from client import ApiError, Cooldown, LedgerClient
from configuration import guardian_config
from guardian_source import GuardianSource
from smoke_test import summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.guardian.json")
    parser.add_argument("--limit", type=int, choices=(1, 2), default=2)
    args = parser.parse_args(argv)
    api = None
    try:
        config = guardian_config(args.config)
        api = LedgerClient(config["LEDGER_BASE_URL"], config["GUARDIAN_API_KEY"], role="guardian")
        snapshots, more = GuardianSource(api).collect(args.limit)
        print(f"Guardian read-only check OK: {len(snapshots)} pending orders read; more={'yes' if more else 'no'}.")
        for item in snapshots:
            print(f"Order #{item.transaction_id}: {item.action} {item.quantity:f} {item.symbol}")
            # Omit the generic smoke-test heading and its final line.
            print("\n".join(summary(item.portfolio, item.policy, item.market, item.symbol).splitlines()[1:-1]))
        if not snapshots:
            print("Queue is empty; no per-order evidence was needed.")
        print("Observation only. Orders remain pending; no model calls or reviews submitted.")
        return 0
    except Cooldown as exc:
        print(f"Ledger cooldown: wait at least {math.ceil(exc.seconds)} seconds before rerunning. No reviews submitted.", file=sys.stderr)
        return 2
    except (ApiError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    sys.exit(main())
