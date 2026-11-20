# COSC 435 AI Security Engineering

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
