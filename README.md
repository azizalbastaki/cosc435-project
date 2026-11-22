# COSC 435 AI Security Engineering

## Submitted theory check

Read your existing submitted theory with the trader key and check its policy
eligibility. Each teammate uses their own config and assigned currency:

```sh
source .venv/bin/activate
python theory_check.py --config config.json --symbol ETH
python -m unittest discover -s tests -v
```

`theory.py` validates the response from `theory.php`: SUBMITTED status, positive
integer ID/version, lookback of 1–1440 minutes, supported strategy and currencies,
required nonempty text without NUL, UTF-8 byte limits, and numeric ranges with at
most four decimal places. Booleans, strings used as numbers, NaN, and infinity
are rejected. Values are stored in an immutable Theory object with Decimal
settings; prose is retained as untrusted data and excluded from its repr.

The API validator accepts one to six distinct currencies. The member-specific
`read_theory` check additionally requires exactly the assigned currency, as the
course requires, and checks `policies.php` for permission. It does not alter
your theory or policy. Other server metadata is ignored; created_at is not used
for decision-making. The summary prints only validated identity/settings.

Exit 0 means this check passed; exit 1 prints a local HOLD for invalid/missing
theory, policy, configuration, or API failure; exit 2 is a cooldown. No HOLD or
trade is sent to Ledger. Passing does not establish profitability or authorize
trading. The offline demo still uses fixtures; wiring theory into a live trader,
market replay/signals, and version snapshots for pending-order reviews remain
later work. Never assume the latest theory matches an older pending order.

The read-only client now permits `theory.php` for either role, as documented by
Ledger; all POST requests remain disabled. No other endpoints were added.
Reference: Student Development Guide Sections 5.1/6.3 and Appendix B.
AI assistance (2026-11-23): Codex implemented theory parsing, the read-only command,
and mocked boundary/field/eligibility tests. Existing smoke tests were updated
to reflect the newly allowed theory GET.

## How to read the code

Start with the agent flow, then follow the helpers as needed:

| Read in this order | What to look for |
| --- | --- |
| `trader.py` → `decide()` | Read evidence → ask the model → validate → return a candidate or HOLD. |
| `guardian.py` → `review_next()` | Read pending order → independent checks → model review → fresh checks. |
| `validation.py` → `_validate()` | Five numbered stages; named helpers check permissions, quotas, cash, and holdings. |
| `offline_demo.py` → `run_demo()` | Connects the two agents through an in-memory queue. |
| `agent_contracts.py` | Expected model JSON fields and evidence selection/validation. |
| `model_adapter.py` | The actual Muse HTTP call and response validation. |

Live read-only tools are separate: `smoke_test.py` reads the trader's account;
`guardian_check.py` uses `guardian_source.py` to inspect pending orders. Both use
`client.py` for HTTP and `configuration.py` to read private settings.

Two Python details used in the code:

- `validate_order(order, **data)` passes the entries of the `data` dictionary as
  named arguments (`policy=...`, `portfolio=...`, and so on).
- `raise ValidationError(...)` stops a check. The caller's `except` block handles
  it as HOLD, BLOCK, or ESCALATE according to the stage that failed.

The functions return data; they do not automatically submit trades. The longer
milestone notes below explain what each part can currently do.

Readability pass (2026-11-22, with Codex assistance): expanded dense expressions,
added numbered agent/validation stages, separated balance and evidence checks,
and removed a redundant wrapper. Existing safety behavior is covered by tests.

## Milestone 5: Guardian reads directly from Ledger

`guardian_source.py` adds read-only pending-order evidence collection.
`guardian_check.py` is the one-shot command. It uses a separate guardian config,
GETs `trade_pending.php?limit=2`, and independently GETs policy, portfolio, and
the relevant market price for each pending order. Embedded queue context, trader
price estimates, and reasoning are not used as evidence or printed. `has_more`
is reported but never followed automatically. An empty queue is successful.

Create a separate ignored file (do not overwrite an existing one):

```sh
cp -n config.guardian.example.json config.guardian.json
chmod 600 config.guardian.json
```

Edit **config.guardian.json**, replacing its placeholder with the complete
guardian-scoped Ledger key. The example must remain placeholders only. Do not
put the trader key in this file; either role loader rejects the other role's key.
The client role restricts endpoints locally; Ledger checks the actual key scope.
A full key may still pass server reads, so verify the scope in the dashboard.

```sh
python guardian_check.py --config config.guardian.json
python -m unittest discover -s tests -v
git check-ignore config.guardian.json
```

This prints a small validated summary, leaves every order pending, and submits
no reviews. It does not call Muse. Exit codes: 0 success, 1 invalid configuration
or API/response failure, 2 cooldown (wait the reported time before rerunning).
An invalid pending row stops the batch; this observer does not decide its fate.
Do not create orders merely to populate the queue for this check.

Live review decisions are not connected yet: we still need theory/version
provenance, full evidence validation, fresh rechecks, and recovery handling before
enabling review submission. `guardian_source.py` is an observational collector,
not a drop-in live replacement for the offline fixture strategy source.
The existing trader client remains limited to its original three GET endpoints;
only a guardian client can now GET pending orders. POST remains disabled for both.

AI assistance (2026-11-22): Codex added the read-only Guardian collector, role
config isolation, command, and mocked tests. AGENTS.md now reflects this expanded
read-only milestone. Only the placeholder guardian example is exempted from Git
ignore; real role configuration remains ignored.

## Milestone 4: offline Trader and Guardian flow

The Guardian reads from Ledger, not from the trader. This milestone models that
boundary using a simulated queue and separate fixture evidence sources:

```text
Trader reads evidence -> model candidate -> local validation
                                  |
                         simulated Ledger queue
                                  |
Guardian reads pending order + its own policy/portfolio/quote/strategy
    -> hard rules -> model review -> fresh evidence + final hard rules
```

`trader.py` and `guardian.py` now implement those offline cycles.
`agent_contracts.py` defines their separate prompts' JSON contracts and selects
the evidence fields allowed into model payloads. `offline_demo.py` supplies the
fake Ledger and test models. No agent can call a Ledger write endpoint here.

Run the deterministic demo (no credentials or network):

```sh
python offline_demo.py
python offline_demo.py --scenario oversized
python offline_demo.py --scenario unsupported-sale
python -m unittest discover -s tests -v
```

Expected outcomes: the normal scripted trader's 0.1 ETH BUY is APPROVE; the
10 ETH BUY and the 0.3 ETH SELL with only 0.2 ETH owned are BLOCK. Attack cases
deliberately inject a pending order past trader validation to prove the Guardian
does its own checks. They do not use the real Ledger Attack Simulator. A demo
APPROVE is a local result only: it creates no fill and changes no balances.

To use actual Muse decisions with the same synthetic Ledger data, explicitly run:

```sh
python offline_demo.py --live-model --config config.json
```

This makes up to two billable model requests, with different role prompts and
fresh model messages for each role. It does not share trader conversation history
with the Guardian. A trader HOLD leaves the queue empty; a Guardian hard-rule
failure skips its model call. Live-model outcomes are not deterministic. The
default scripted mode is only a test double, not evidence of actual LLM use.

The Guardian ignores claimed trader validation, requested prices, and estimated
amounts. It independently calculates amounts, checks policy/quotas/funds/holdings,
and escalates at or above the configured threshold. MODIFY must strictly decrease
quantity. APPROVE/MODIFY trigger another read of policy, portfolio, quote, and
strategy before the final check. Model faults or missing evidence produce a local
ESCALATE; trader model/validation faults produce a local HOLD. Raw model reasoning
and full source replies are not printed by the demo.

These are testable agent building blocks, not the completed live system. The
fixture strategy is not a submitted theory; active-theory validation and matching
version snapshots, replay/signals/news, persisted proposal/review recovery,
separate live role credentials, polling/cooldown handling, and live transport
remain to be implemented. In a later live milestone, the Guardian will use its
guardian-scoped key to GET `trade_pending.php`, `policies.php`, `portfolio.php`,
and `market_prices.php` directly from Ledger, then submit its review only after
the final recheck. No new live endpoints are enabled in this milestone.

AI assistance (2026-11-22): Codex implemented the separate offline agent cycles,
simulated queue, independent evidence reads, and tests for attacks and evidence
changing while the model reviews an order.

## Milestone 3: Muse model adapter

`model_adapter.py` provides `MuseAdapter.generate(system_prompt, payload, schema)`.
It calls Meta's model API, then validates the JSON response locally. This is the
shared model transport for the trader and Guardian. Their offline decision cycles
are described above; live Ledger integration is still pending.

The ignored `config.json` may now include the four Muse fields shown in
`config.example.json`: `LLM_PROVIDER` (`meta_muse`), `META_BASE_URL`
(`https://api.meta.ai/v1`), `MUSE_MODEL` (`muse-spark-1.3`), and `META_API_KEY`.
Keep your real values only in local config. Do not overwrite an existing private
config with the template. Ledger-only configs still work for the Ledger check.
`configuration.py` accepts only known string fields and rejects duplicate names;
the Ledger loader returns only its own two settings. Guardian credentials will
use a separate role config in a later step.

```sh
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To explicitly make **one billable model request** with fixed synthetic data:

```sh
python model_smoke_test.py --config config.json
```

Success prints only that the structured response validated. No Ledger requests,
portfolio data, trading proposals, or reviews are involved. Exit 1 means config,
provider access, or response validation failed. The program does not retry.

The adapter follows Appendix C's Meta example, using the OpenAI-compatible SDK
with Meta's base URL (the request goes to Meta, not OpenAI). It requests a strict
JSON schema, uses a 30-second timeout and zero SDK retries, disables redirects
and ambient proxies, and rejects refusals, truncated output, malformed JSON,
duplicate fields, non-finite numbers, and schema mismatches. Errors never echo
provider bodies. It also restricts the destination to Meta and rejects known
configured secrets or credential-like payload field names before sending.
These checks do not replace constructing prompts from explicitly selected data;
never pass configuration or headers as model input. The key is used only for
HTTP authentication. Ledger keys never belong in model messages.

The calling trader must HOLD on `ModelError`; the future Guardian must ESCALATE
when Ledger is available, or leave the order pending during Ledger cooldown.
Schema validation is only a format check; local order/Guardian rules still apply.

Reference: [Meta structured output documentation](https://dev.meta.ai/docs/structured-output).
AI assistance (2026-11-22): Codex added the adapter, separate model connection
check, shared config parsing, and mocked failure/credential-isolation tests.

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
The original trader.py and guardian.py scaffolds now host the offline cycles
described above; market.py remains empty for the replay milestone.

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
- Tightened URL/key checks, added an 8 KiB configuration cap with known
  string fields (now including optional Muse settings), and validated displayed response fields. The summary replaces
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
