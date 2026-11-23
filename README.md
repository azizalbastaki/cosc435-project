# COSC 435 AI Security Engineering

Two agents work with the course's simulated Ledger account:

1. The **trader** reads its submitted theory and market/account data, asks Muse for
   a candidate, and checks the candidate locally.
2. The **Guardian** reads the pending order directly from Ledger, gathers its own
   evidence, applies hard rules, asks Muse, and repeats the checks before review.

The Guardian does not trust the trader's estimates or validation result. Both
agents use the same pure calculation functions with their own evidence.

## Setup

Use Python 3.10+ on macOS/Linux. From this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n config.example.json config.json
cp -n config.guardian.example.json config.guardian.json
chmod 600 config.json config.guardian.json
```

Edit the **local** files, not the examples:

- `config.json`: trader-scoped Ledger key and Muse settings.
- `config.guardian.json`: guardian-scoped Ledger key and Muse settings.
- The Meta provider key can be shared by both agents; their Ledger keys must be
  separate. Never use Ledger's `full` scope.

Examples contain placeholders and can be committed. Real config files, `.venv/`,
`.ledger-state/`, and the local course PDFs in `docs/` are Git-ignored.
Never share keys in chat, screenshots, model prompts, or source control.

Use `.venv/bin/python` in the commands below. A `jsonschema` import error usually
means you used a different Python interpreter.

## Run one cycle

Start with the default **dry run**. It reads Ledger and makes a billable Muse
request but submits nothing:

```sh
.venv/bin/python trade_once.py --symbol ETH
```

To explicitly allow **at most one simulated Ledger proposal and Guardian review**:

```sh
.venv/bin/python trade_once.py --symbol ETH --submit
```

This can change your simulated portfolio. It still returns HOLD if the theory
or safety checks do not justify an order. There is no automatic loop.
Teammates use their own accounts/config files and substitute their assigned symbol:
BTC, ETH, DOGE, XMR, XRP, or SOL.

The submission flow checks existing pending/escalated work, verifies the two keys
see the same submitted theory, obtains a trader decision, refreshes the evidence,
records the intent, and submits once. The Guardian independently checks that exact
transaction and refreshes its evidence before review. The final result comes from
Ledger history; an APPROVE decision alone does not prove a fill occurred.

## Simple recovery file

State is readable JSON at **`.ledger-state/trader.json`**. Each entry records the
currency, action, quantity, theory ID/version, transaction ID, and state. It does
not store keys, model reasoning, or theory prose.

```text
PLANNED -> UNCERTAIN -> PENDING -> EXECUTED / BLOCKED / REJECTED
                            -> ESCALATED (still unresolved)
```

- Save the intent before sending. Mark UNCERTAIN immediately before the request.
- A lost response must not cause a second order. Unresolved work blocks proposals,
  including after restart. An unsent PLANNED intent can be cancelled locally.
- A saved transaction ID lets the program check Ledger history for the outcome.
- If the ID was lost, or a review was attempted but remains pending, stop and
  investigate. The program does not guess or blindly resend.

Check history without model calls or writes:

```sh
.venv/bin/python trade_once.py --reconcile-only
```

Keep the same journal for an account. A stored credential fingerprint prevents
accidentally reusing it with another key/account; key rotation needs deliberate
migration after resolving old work. Deleting state is not reconciliation.

Saving uses an owner-only temporary file and atomic replacement. A small `.lock`
file prevents two local runs at once; the OS releases the lock when a run exits.
Malformed state stops the program rather than being treated as empty. An existing
SQLite journal is not silently replaced: it requires deliberate migration. No
SQLite journal existed locally when this project switched to JSON.

Offline restart/lost-response demo:

```sh
.venv/bin/python recovery_demo.py
```

## Other useful commands

| Command (after `.venv/bin/python`) | What it does |
| --- | --- |
| `smoke_test.py --symbol ETH` | Reads portfolio, policy, and price. |
| `theory_check.py --symbol ETH` | Checks submitted theory and policy eligibility. |
| `market_check.py --symbol ETH` | Reconstructs 61 prices and checks replay against the current quote. |
| `model_smoke_test.py` | One small billable Muse connection test; no Ledger calls. |
| `trader_dry_run.py --symbol ETH` | Live trader analysis without submission. |
| `guardian_check.py` | Reads up to two pending orders and their independent evidence. |
| `guardian_dry_run.py` | Locally reviews one pending order; never submits the review. |
| `offline_demo.py` | Scripted two-agent demonstration with no network. |
| `offline_demo.py --scenario oversized` | Injects an oversized order into the fake queue. |
| `offline_demo.py --scenario unsupported-sale` | Tests selling more than the fake holdings. |

`offline_demo.py --live-model` uses billable Muse calls with synthetic Ledger data.
An empty live Guardian queue is normal: NO_PENDING requires no model call.
Live model calls send selected theory text and simulated-account evidence to Meta,
never Ledger keys. Console summaries exclude raw model reasoning and server bodies.

## Where to read the code

| File | Responsibility |
| --- | --- |
| `trade_once.py` | Coordinates one dry-run or explicitly enabled submission cycle. |
| `trader_dry_run.py` | Live trader evidence, model decision, and local validation. |
| `guardian.py` | Guardian rules, model review, and final recheck. |
| `guardian_live.py` | Independent live Guardian evidence and matching theory version. |
| `strategy.py` | Shared signal directions, stop/take-profit checks, and size bounds. |
| `validation.py` | Numeric, permission, quota, money, and holdings checks. |
| `theory.py`, `market.py` | Submitted theory validation and public price replay. |
| `agent_contracts.py` | Model JSON contracts and selected evidence fields. |
| `model_adapter.py`, `client.py` | Muse and Ledger HTTP transports. |
| `configuration.py`, `recovery.py` | Private config loading and persistent JSON state. |
| `trader.py`, `offline_demo.py` | Small fixture-based demonstration flow. |

## Course rules and implementation choices

The API contract comes from the Student Development Guide, especially Sections
6.2–6.7, 7.4, and Appendices A–C.

- HTTP follows the guide scaffold: HTTPS, no redirects, X-Api-Key authentication,
  5/15-second timeouts, 1.1-second spacing, bounded GET transport retries, and shared
  429/503 cooldowns. POST requires explicit opt-in and the correct role endpoint,
  a finite JSON object below 32 KiB, and is never automatically retried. Lost,
  unreadable, or 5xx write responses are uncertain until reconciled.
- Differences from the scaffold: no raw error bodies, stricter config/URL checks,
  HTTP-date Retry-After support, disabled ambient proxies/.netrc, and explicit
  client cleanup. Separate configs reject the other role's Ledger key.
- Money uses Decimal. BUY settles upward to cents; SELL settles downward. Policy
  checks use the larger exact/settled amount. MODIFY can only reduce quantity.
- Theory checks enforce SUBMITTED status, identity/version, bounded settings and
  text. The member command requires exactly their assigned currency. Text remains
  untrusted data even when it passes format checks.
- Replay follows Appendix A's operation order and integer noise. It uses 61 past
  samples bounded by the quote timestamp, refreshes once on mismatch, then HOLDs.
  The descriptor is cached for five minutes when a reader is reused. Test fixtures
  contain selected public BTC/ETH parameters fetched on 2026-11-23, never secrets.
  Synthetic future-day fixtures are tests only, not live decision evidence.
- Momentum uses lookback return; mean reversion uses negative mean deviation.
  A tiny signal-only tolerance never relaxes money limits. Risk exits have priority.
- Muse replies must pass JSON schema and local rules. Failure produces trader HOLD
  or local Guardian ESCALATE; Ledger cooldown leaves pending work untouched.
- Guardian hard-rule blocks name the first failed check and its phase, for example
  `Initial hard-rule check failed: position_fraction.` or
  `Final hard-rule check failed: strategy_quantity.` These fixed local labels
  distinguish cash, policy, quota, and strategy failures without printing supplied
  inputs or exception text. Existing review reasons carry this diagnostic; old
  generic block records cannot be diagnosed retroactively. This is a local
  scaffold adaptation added with Codex assistance, not a new API field.
- SQLite was an implementation choice, not a course requirement. JSON persistence
  now provides the required save-before-send and remember-after-restart behavior.

## Testing and current limits

Run the repeatable offline security demonstration:

```sh
.venv/bin/python security_demo.py
.venv/bin/python security_demo.py --output security-evidence.json
```

It checks a valid control plus three scenarios: an oversized order claiming prior
approval, a sale exceeding owned currency, and a policy limit tightened during
model review. Each case prints PASS/FAIL and the Guardian's reason; any failure
returns a nonzero exit code. No configuration or API access is needed. These
scripted-model results demonstrate local safeguards, not live Muse resistance to
prompt injection or completed course trading requirements. Codex assisted with
this demo and its tests; retain that distinction in the course report.

The optional JSON export records the UTC run time, synthetic setup, expected and
actual decisions/reasons, model-call counts, and which control was evaluated.
Transaction IDs are null because no Ledger transaction exists. It refuses to
overwrite a file; choose a new filename for another run. This supports the
simulation evidence requested in Guide Section 8.2, but does not establish live
trading results or server-backstop behavior. The file contains only synthetic
fixtures and can be reviewed and committed as offline evidence.

```sh
.venv/bin/python -m unittest discover -s tests -v
git diff --check
git status --short
```

Tests use mocked transport/models and temporary state files. They cover replay
fixtures, validation boundaries, independent Guardian reads, changes during review,
uncertain writes, restarts, locking, and safe persistence. No live trade is used as
an automated test. Review the diff before committing; examples must stay placeholders.

Still outstanding: historical theory snapshots, an explicit uncertain-ID resolution
workflow, automated retry of a still-pending review after fresh reconciliation,
repeated agent scheduling, full evaluation/audit reporting, and the final report
and presentation. A missing old theory or unsupported custom strategy escalates
or HOLDs instead of guessing. The journal searches only the latest 200 history
records for a known ID; absent records remain unresolved. It cannot coordinate
other devices or clients that use a different journal.

AI assistance: Codex helped adapt the guide, implement the agents and tests, and
simplify the code/documentation during 2026-11-20 through 2026-11-23. Include accurate
AI-use disclosure and named team contributions in the course report. Report the
public deterministic price generator: simulated profit is not forecasting proof.
