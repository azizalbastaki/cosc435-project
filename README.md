# COSC 435 AI Security Engineering

## Milestone 2: offline local order validation

`validation.py` now provides `validate_order`, a pure function for checking a
trader candidate against supplied policy, portfolio, quote, selected currencies,
and per-buy position fraction. It makes no HTTP requests and reads no keys.
The smoke test below remains unchanged; no trading loop is connected yet.

The candidate must contain exactly `symbol`, `action`, `quantity`, and `reasoning`.
BUY/SELL checks cover currency eligibility, restricted actions, executed hourly
and daily quotas, positive finite quantities, the transaction ceiling, available
cash/per-buy cash fraction, and owned SELL quantity. Missing holdings mean zero.
HOLD requires zero quantity and a nonempty explanation, then returns locally
without requiring trading context. A bad candidate raises `ValidationError`;
a later trader loop should handle that as a local HOLD, never submit it.

Example, entirely offline (run in Python from the repository root):

```python
from validation import validate_order

checked = validate_order(
    {"symbol": "ETH", "action": "BUY", "quantity": 0.1,
     "reasoning": "Illustrative candidate for testing only."},
    policy={"allowed_symbols": ["ETH"], "restricted_actions": ["WITHDRAW"],
            "max_transaction_amount": 1000, "max_trades_per_hour": 10,
            "max_trades_per_day": 50, "trades_last_hour": 0, "trades_last_day": 0},
    portfolio={"cash_balance": 10000, "holdings": []},
    quote={"symbol": "ETH", "price": 3200},
    selected_symbols=["ETH"], position_fraction=0.05,
)
print(checked.action, checked.quantity, checked.settled_amount)
# BUY 0.1 320.00 — nothing is submitted.
```

Run just these tests, or the complete suite:

```sh
python -m unittest discover -s tests -p 'test_validation.py' -v
python -m unittest discover -s tests -v
git diff --check
```

Money helpers adapt Section 6.3 of the Student Development Guide: BUY settlement
rounds up, SELL settlement rounds down, and policy checks compare the larger of
the exact and settled amounts against the ceiling. `bounded_quantity` rounds
budget down to cents and quantity down to ten decimals. The helpers add strict
numeric checks, explicit BUY/SELL action checks, and a local 80-digit Decimal
context so ambient precision cannot alter the calculation. Policy lists accept
arrays or comma-separated text. Numeric strings and booleans are rejected;
internal Decimal values are accepted alongside JSON integers/floats. For this
milestone, supplied amounts/prices also conservatively use the quantity bounds
(below 10^18, at most ten fractional places). No tolerance relaxes money limits.

This is a building block, not complete theory validation or permission to trade.
Before live proposals, the caller must validate an active submitted theory,
supply its selected symbols and position fraction, attach its id/version, verify
fresh market evidence and strategy compliance, and handle pending/uncertain
orders. Quote freshness and those theory checks are not implemented here.
The Guardian must independently fetch and check evidence later; the trader's
successful validation is not Guardian approval. No new API endpoints were added.

AI assistance (2026-11-22): Codex implemented and tested these offline checks
using guide Sections 6.3–6.5. Include team review and this assistance in the final
report disclosure. Review `validation.py`, `tests/test_validation.py`, and this
section before committing this milestone.

## Milestone 1: connection check

Milestone 1: a one-shot, read-only Ledger smoke test. It reads the portfolio,
security policy, and selected currency's market price with one trader client.
It needs no active theory and sends no orders, reviews, or HOLD entries.
The existing trader.py, guardian.py, and market.py remain empty for later steps.

## Setup and run

Use Python 3.10 or newer. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

Edit `config.json` locally. Copy the HTTPS API base URL from Ledger's API Keys
page and replace the placeholder with your complete **trader-scoped** key.
Do not use a guardian or full key. The GET endpoints do not prove the scope of
an otherwise valid key; verify its scope in the dashboard. Never paste the key
into chat, command-line arguments, screenshots, or source control.

```sh
python smoke_test.py --config config.json --symbol ETH
```

ETH is the default. Teammates can select BTC, ETH, DOGE, XMR, XRP, or SOL
(case-insensitive). Each teammate uses their own local configuration and key.
For example: `python smoke_test.py --config config.local.json --symbol SOL`.
Keep private files named `config.json` or `config.<name>.json` in this repo so
the ignore rules cover them; only `config.example.json` is committed.

The summary shows cash, total portfolio value, selected holdings and quote,
policy eligibility, transaction ceiling, executed trade counts/limits, escalation
threshold, and restricted-action count. An unowned currency has zero holdings;
a policy-disallowed currency is reported as such and is still safe to read.
Missing/malformed quote data fails the check. Values are snapshots, not proof
that a future trade is permitted. No active theory is fetched or required.

Exit codes: 0 = three reads and summary validated; 1 = configuration, transport,
API, or response validation failure; 2 = cooldown. For 429/503, wait at least
the reported duration before rerunning. The client blocks further requests for
that cooldown. A new process has no persisted timer, so honor the printed wait.
401 means check the full original token; 403 means check the required scope.

## Offline verification

```sh
python -m unittest discover -s tests -v
git diff --check
```

Tests mock the HTTP session, clock, and sleeps: no Ledger credentials or network
are used. They cover the three GETs, ETH/other currencies, absent holdings,
policy exclusion, invalid configuration, unsafe URLs, write rejection, pacing,
redirect refusal, HTML/non-object replies, API errors, bounded network retries,
429/503 cooldowns, and output secrecy. Live connectivity is a separate manual
check using the command above; mocked success does not establish live access.

## Course scaffold adaptations

`client.py` adapts the Student Development Guide Section 6.2 scaffold (PDF pages
7–8). Response parsing uses Appendix B (pages 20–22), query parameters use
Section 7.2, and configuration names use Appendix D.

- Retained requests.Session, X-Api-Key authentication, HTTPS validation,
  disabled redirects, 5/15-second timeouts, 1.1-second request spacing,
  three attempts for transport-failed GETs, jitter, and shared cooldown state.
- Restricted the transport to GET and the three smoke-test endpoints. Removed
  POST/body serialization and UncertainWrite for this milestone. Restore those
  safeguards deliberately with write/reconciliation tests in a later milestone.
- Errors expose HTTP status and a constrained error code, never raw bodies,
  server messages, or underlying network exception text. HTML errors point to
  the base URL/hosting layer. Unknown nonconforming error codes are suppressed.
- Added HTTP-date Retry-After support alongside seconds and the guide's
  15-second fallback. The CLI exits on cooldown instead of running a loop.
- Disabled ambient proxies/.netrc via session.trust_env=False and added session
  cleanup. Networks requiring a proxy/custom CA need an explicit future setup.
- Tightened URL/key checks, added an 8 KiB configuration cap with exactly two
  string fields, and validated displayed response fields. The summary replaces
  the scaffold's raw dictionary printing; monetary display uses Decimal.

## Before committing

Review the three-endpoint restriction, config placeholders, summary field mapping,
and test results. Check `git status --short` and `git diff`; new files also need
review because they do not appear in an unstaged diff until added. Verify ignores:

```sh
git check-ignore config.json config.local.json .venv/
git status --short
```

The existing `/docs` ignore rule is preserved. Course PDFs remain local.
Do not stage real credentials. No commit or live Ledger check is automated.

AI assistance: Codex helped read the supplied course PDFs, adapt the scaffold,
and implement/document/mock-test this milestone on 2026-11-20. The team should
review this work and include an accurate AI-use disclosure and named member
contributions in the final report, as required by both course documents.
