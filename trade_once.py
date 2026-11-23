"""One controlled lifecycle. Dry-run by default; --submit explicitly enables writes."""

import argparse
from contextlib import ExitStack
import hashlib
import math
import sys

from client import ApiError, Cooldown, LedgerClient, UncertainWrite
from configuration import guardian_config, ledger_config
from guardian import review_next
from guardian_live import LiveGuardianSource
from market import MarketReader
from model_adapter import ModelError, MuseAdapter
from recovery import ProposalJournal, RecoveryError, positive_id
from theory import read_theory
from trader_dry_run import decide_live, decision_bounds
from validation import SYMBOLS, ValidationError, validate_order


def history(api, **params):
    rows = api.get("transactions.php", **params).get("transactions")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RecoveryError("Invalid Ledger history response")
    return rows


def reconcile(api, journal):
    saved = journal.open_proposal()
    if saved is None:
        return None
    if saved["transaction_id"] is None:
        raise RecoveryError("Unresolved local intent without an ID: investigate before sending anything")
    rows = history(api, limit=200)
    matches = [row for row in rows if row.get("transaction_id") == saved["transaction_id"]]
    if len(matches) != 1:
        raise RecoveryError("Known transaction is absent or ambiguous in recent history; do not resend")
    journal.reconcile_known(saved["intent_id"], matches[0])
    return journal.get(saved["intent_id"])


def require_clear_account(api):
    for status in ("PENDING", "ESCALATED"):
        if history(api, status=status, limit=1):
            raise RecoveryError("Account has pending/escalated work; resolve it before proposing")


def recheck_candidate(api, order, theory):
    """Repeat trader checks after its model call with a fresh quote and balances."""
    current = read_theory(api, order.symbol)
    if current != theory:
        raise ValidationError("Theory changed during the trader decision")
    policy = api.get("policies.php")
    portfolio = api.get("portfolio.php")
    signals = MarketReader(api).read(order.symbol, theory.lookback_minutes)
    bounds = decision_bounds(theory, signals, policy, portfolio, order.symbol)
    if order.action not in bounds["allowed_actions"]:
        raise ValidationError("Fresh evidence no longer supports this action")
    maximum = bounds["max_buy_quantity"] if order.action == "BUY" else bounds["max_sell_quantity"]
    if order.quantity > maximum:
        raise ValidationError("Fresh evidence reduced the allowed quantity")
    return validate_order(
        {"symbol": order.symbol, "action": order.action, "quantity": order.quantity, "reasoning": order.reasoning},
        policy=policy, portfolio=portfolio,
        quote={"symbol": order.symbol, "price": signals.current_price},
        selected_symbols=list(theory.selected_symbols), position_fraction=theory.position_fraction,
    )


def review_known(trader_api, guardian_api, guardian_model, journal, saved):
    intent = saved["intent_id"]
    transaction_id = saved["transaction_id"]
    if saved["state"] != "PENDING":
        return f"Order #{transaction_id}: {saved['state']} (history confirmed)"
    if journal.review_attempted(intent):
        raise RecoveryError("Review already attempted and outcome still pending; investigate, do not resend")

    # Guardian fetches its own queue, theory, policy, portfolio, replay, and quote.
    source = LiveGuardianSource(guardian_api, target_id=transaction_id)
    result = review_next(source, guardian_model)
    if source.order is None or result["decision"] == "NO_PENDING":
        raise RecoveryError("Guardian could not identify this pending order; leave it unresolved")
    # A successful final read from the Guardian is immediately followed by review.
    body = {"transaction_id": transaction_id, "decision": result["decision"],
            "reason": result["reason"], "rules_triggered": []}
    if result["decision"] == "MODIFY":
        body["modified_quantity"] = format(result["quantity"], "f")
    journal.mark_review_attempt(intent, result["decision"])
    reply = guardian_api.post("trade_review.php", body)
    if reply.get("transaction_id") != transaction_id or reply.get("status") not in (
            "EXECUTED", "BLOCKED", "REJECTED", "ESCALATED"):
        raise UncertainWrite("Unexpected review reply; reconcile history before proceeding")
    # The history record, including fill data, is authoritative, not APPROVE.
    final = reconcile(trader_api, journal)
    return f"Order #{transaction_id}: {final['state']} (history confirmed)"


def submit_once(trader_api, guardian_api, trader_model, guardian_model, journal, symbol):
    # Restart recovery always precedes new decisions. Never make a new proposal
    # in the same invocation that reconciles a prior one.
    saved = reconcile(trader_api, journal)
    if saved is not None:
        return review_known(trader_api, guardian_api, guardian_model, journal, saved)
    require_clear_account(trader_api)

    # Check that the two role credentials see the same account's submitted theory.
    trader_theory = read_theory(trader_api, symbol)
    guardian_theory = read_theory(guardian_api, symbol)
    if (trader_theory.id, trader_theory.version) != (guardian_theory.id, guardian_theory.version):
        raise RecoveryError("Trader and Guardian theory identities differ; check account keys")
    guardian_api.get("trade_pending.php", limit=1)  # Verify Guardian scope before any proposal.
    order, theory, _ = decide_live(trader_api, trader_model, symbol)
    if order.action == "HOLD":
        return "HOLD: no proposal submitted"

    require_clear_account(trader_api)
    order = recheck_candidate(trader_api, order, theory)
    intent = journal.reserve(order, theory.id, theory.version)
    body = {"symbol": order.symbol, "action": order.action,
            "quantity": format(order.quantity, "f"), "theory_id": theory.id,
            "reasoning": order.reasoning}
    journal.mark_attempt(intent)
    reply = trader_api.post("trade_propose.php", body)
    transaction_id = positive_id(reply.get("transaction_id"))
    # Save the returned ID before validating optional details, so recovery can
    # look up this exact order if the remainder of the response is malformed.
    journal.record_response(intent, transaction_id, reply.get("status"))
    if (reply.get("theory_id") != theory.id or reply.get("theory_version") != theory.version
            or reply.get("symbol") != order.symbol or reply.get("action") != order.action):
        raise RecoveryError("Proposal reply does not match the intent; reconcile before review")
    return review_known(trader_api, guardian_api, guardian_model, journal, journal.get(intent))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit", action="store_true", help="Submit at most one simulated Ledger order and review")
    mode.add_argument("--reconcile-only", action="store_true", help="Read history only; never write or call models")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--guardian-config", default="config.guardian.json")
    parser.add_argument("--journal", default=".ledger-state/trader.json")
    parser.add_argument("--symbol", type=str.upper, choices=sorted(SYMBOLS), default="ETH")
    args = parser.parse_args(argv)
    with ExitStack() as stack:
        try:
            config = ledger_config(args.config)
            trader_api = LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"], allow_writes=args.submit)
            stack.callback(trader_api.close)
            if args.submit or args.reconcile_only:
                journal = ProposalJournal(args.journal)
                stack.callback(journal.close)
                identity = config["LEDGER_BASE_URL"].rstrip("/") + "\0" + config["TRADER_API_KEY"]
                journal.bind_identity(hashlib.sha256(identity.encode()).hexdigest())
                if args.reconcile_only:
                    saved = reconcile(trader_api, journal)
                    print("No open local proposal." if saved is None else f"History status: {saved['state']}")
                    return 0
            trader_model = MuseAdapter.from_config(args.config)
            stack.callback(trader_model.close)
            if not args.submit:
                order, theory, _ = decide_live(trader_api, trader_model, args.symbol)
                print(f"DRY RUN: {order.action} {order.quantity:f} {args.symbol}; theory {theory.id}/v{theory.version}. Nothing submitted.")
                return 0
            guardian = guardian_config(args.guardian_config)
            if guardian["LEDGER_BASE_URL"].rstrip("/") != trader_api.base:
                raise RecoveryError("Role configurations must use the same Ledger base URL")
            guardian_api = LedgerClient(guardian["LEDGER_BASE_URL"], guardian["GUARDIAN_API_KEY"], role="guardian", allow_writes=True)
            stack.callback(guardian_api.close)
            guardian_model = MuseAdapter.from_config(args.guardian_config, role="guardian")
            stack.callback(guardian_model.close)
            print(submit_once(trader_api, guardian_api, trader_model, guardian_model, journal, args.symbol))
            return 0
        except Cooldown as exc:
            print(f"STOP: cooldown; wait at least {math.ceil(exc.seconds)} seconds and reconcile any open intent.", file=sys.stderr)
            return 2
        except (RecoveryError, UncertainWrite) as exc:
            print(f"STOP: {exc}", file=sys.stderr)
            return 1
        except (ApiError, ModelError, ValueError, KeyError, TypeError, OSError):
            print("STOP: validation, storage, model, or API failure. Check the journal before another submission.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
