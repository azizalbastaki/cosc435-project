"""Bounded live-data trader monitor. Never submits orders or reviews."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import math
import time

from client import ApiError, Cooldown, LedgerClient
from configuration import ledger_config
from market import MarketReader
from model_adapter import ModelError, MuseAdapter
from trader_dry_run import decide_live, failure_message
from validation import SYMBOLS


def watch(api, model, symbol, cycles, interval):
    reader = MarketReader(api)
    for index in range(cycles):
        order, theory, signals = decide_live(api, model, symbol, reader)
        timestamp = datetime.fromtimestamp(signals.timestamp, timezone.utc).isoformat()
        signal = signals.strategy_signal(theory.strategy)
        print(f"[{index + 1}/{cycles}] {timestamp} | {symbol} | "
              f"{theory.strategy} {theory.lookback_minutes}m signal={signal:.4f}% | "
              f"threshold={theory.threshold_pct}% | {order.action} {order.quantity:f} | "
              "nothing submitted", flush=True)
        if index + 1 < cycles:
            time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--symbol", type=str.upper, choices=sorted(SYMBOLS), default="ETH")
    parser.add_argument("--cycles", type=int, choices=range(1, 11), default=5)
    args = parser.parse_args(argv)
    print("Read-only monitor: up to one billable Muse call per check; Ctrl+C stops.", flush=True)
    try:
        with ExitStack() as stack:
            config = ledger_config(args.config)
            api = LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"])
            stack.callback(api.close)
            model = MuseAdapter.from_config(args.config)
            stack.callback(model.close)
            watch(api, model, args.symbol, args.cycles, interval=60)
        return 0
    except KeyboardInterrupt:
        print("Stopped. No orders submitted.")
        return 130
    except Cooldown as exc:
        print(f"Stopped for cooldown. Wait at least {math.ceil(exc.seconds)} seconds before restarting.")
        return 2
    except (ApiError, ModelError, ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Stopped: {failure_message(exc)} No orders submitted.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
