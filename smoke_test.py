"""One-shot Ledger connectivity check; no theory or trading operations."""

import argparse
import math
import sys
from decimal import Decimal

from client import ApiError, Cooldown, LedgerClient
from configuration import ledger_config as load_config

SYMBOLS = ("BTC", "ETH", "DOGE", "XMR", "XRP", "SOL")


def number(obj, field):
    value = obj[field]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError
    return Decimal(str(value))


def summary(portfolio, policy, market, symbol):
    """Validate displayed Appendix B fields; never dump arbitrary response text."""
    try:
        cash = number(portfolio, "cash_balance")
        total = number(portfolio, "total_value")
        holdings = portfolio["holdings"]
        prices = market["prices"]
        allowed = policy["allowed_symbols"]
        restricted = policy["restricted_actions"]
        if (not isinstance(holdings, list) or not isinstance(prices, list)
                or not isinstance(allowed, list) or not isinstance(restricted, list)
                or any(not isinstance(s, str) for s in allowed + restricted)
                or any(not isinstance(row, dict) for row in holdings + prices)):
            raise ValueError
        owned = [row for row in holdings if row.get("symbol") == symbol]
        quotes = [row for row in prices if row.get("symbol") == symbol]
        if len(owned) > 1 or len(quotes) != 1:
            raise ValueError
        quantity = number(owned[0], "quantity") if owned else Decimal(0)
        price = number(quotes[0], "price")
        if price <= 0:
            raise ValueError
        cap = number(policy, "max_transaction_amount")
        threshold = number(policy, "escalation_threshold")
        counts = []
        for field in ("trades_last_hour", "max_trades_per_hour", "trades_last_day", "max_trades_per_day"):
            if type(policy[field]) is not int or policy[field] < 0:
                raise ValueError
            counts.append(policy[field])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Unexpected Ledger response fields; compare with Appendix B") from None
    hour, hour_limit, day, day_limit = counts
    return "\n".join([
        f"Read-only Ledger check OK | {symbol}",
        f"Portfolio: cash ${cash:,.2f} | total ${total:,.2f} | {symbol} held {quantity:f}",
        f"Market: {symbol} ${price:,.8f}",
        f"Policy: {symbol} allowed={'yes' if symbol in allowed else 'no'} | max transaction ${cap:,.2f}",
        f"Policy: trades hour {hour}/{hour_limit} | day {day}/{day_limit} | escalation ${threshold:,.2f}",
        f"Restricted actions: {len(restricted)} | No orders or reviews submitted.",
    ])


def run(api, symbol):
    portfolio = api.get("portfolio.php")
    policy = api.get("policies.php")
    market = api.get("market_prices.php", symbols=symbol)
    return summary(portfolio, policy, market, symbol)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--symbol", type=str.upper, choices=SYMBOLS, default="ETH")
    args = parser.parse_args(argv)
    api = None
    try:
        config = load_config(args.config)
        api = LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"])
        print(run(api, args.symbol))
        return 0
    except Cooldown as exc:
        print(f"Ledger HTTP {exc.status}: cooldown; wait at least {math.ceil(exc.seconds)} seconds before rerunning.", file=sys.stderr)
        return 2
    except (ValueError, ApiError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    sys.exit(main())
