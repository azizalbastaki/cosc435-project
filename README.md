# TradeSentry

**Trade intelligently. Stay protected.**

Autonomous trading with independent AI security supervision.

## Start here (teammates)

## Supported strategies

The Trader selects **mean reversion** or **momentum** from the validated active
Ledger theory, never from a model reply or a local strategy override. An optional
`expected_theory.strategy` pin must match that theory. Custom strategies still HOLD.

- Momentum: `(current price / price at the exact lookback start - 1) * 100`.
  BUY at or above the positive threshold; SELL an existing position at or below
  the negative threshold. Equality qualifies. Stop-loss and take-profit SELLs
  take priority over either signal. No holdings means no SELL.
- Mean reversion: the existing minute-sampled lookback-mean strategy and its
  validated cycle-window constraints remain in place. Its 24-hour presets and
  suggested trading windows below are **not momentum presets**. The existing
  minute-sampled mean differs from the guide's generic 61-point mean; this change
  preserves that researched strategy rather than silently changing its baseline.

Momentum accepts the guide's 1–1440 minute lookback and 0.01–50% threshold, without
the mean-reversion-only two-hour divisibility rule. Shared local safety limits
remain: position fraction 0.001–0.2, stop loss 1–20%, take profit 5–100%, plus all
order, cash, holdings, quota, and policy checks. Guardian reads its own matching
theory snapshot, portfolio/history, and descriptor, then independently calculates
momentum from the order time and exact lookback start. Mean-reversion slack is not
applied to momentum. Shared pure math does not share credentials or fetched evidence.

For momentum, save and replay your own submitted theory export first. The existing
`data/theory_v1.json` and dashboard Setup presets remain mean reversion. In the
private Trader config set `expected_theory.strategy` to `momentum`, the matching
`version`, and `params` to your validated numeric settings. This pin only checks
Ledger's theory; it does not change the strategy on Ledger. Avoid saving the
mean-reversion dashboard Setup over those settings.

The CLI configuration tool accepts `--strategy momentum --no-pin`; this generates
role-separated config skeletons without borrowing the mean-reversion numeric
presets. Add your replay-validated parameter pins before trading. It overwrites
local configs, so edit existing configs directly if you need to preserve them.
No private configs or saved trading state were changed by this implementation.

Offline replay example (use your own saved theory file and evaluation dates):

```sh
.venv/bin/python -m trading_agent.replay --symbol ETH \
  --model-file data/market_model.json --theory-file /path/to/saved-theory.json \
  --start 2026-11-20T00:00 --end 2026-11-22T00:00
```

Replay validates the theory and descriptor, rejects insufficient pre-epoch history,
and uses the same strategy code as the Trader. The model payload labels the strategy
and supplies its relevant return or mean deviation. Models still may only HOLD or
choose the locally recommended action within the existing size bounds. Recovery,
role separation, model validation, price matching, and final monetary/policy
rechecks remain in place. Simulator replay is not proof of real-world forecasting.

Codex assisted with momentum support, consolidation of signal/risk-exit and
average-cost logic, configuration, and offline tests. Disclose this in the report.

1. **On Ledger (your own account):** submit and activate your coin's theory (text in `docs/Ledger_theories_all_coins.pdf`,
   rewrite the hypothesis in your own words, tick ONLY your coin). Set the Security Policy (your coin only, max $9,500,
   6/hour, 30/day, escalate above $20,000, restrict WITHDRAW). Create a **trader** key and a **guardian** key.
2. **Open the dashboard:** double-click **Start Dashboard.command** (Mac) or **Start Dashboard.bat** (Windows).
   First launch installs libraries (needs internet, ~1 min). If macOS blocks it: right-click > Open.
3. **Setup screen:** your name, your coin, theory version, trader key label; paste your trader and Guardian keys
   (leave the model key box EMPTY - the team Muse key is already saved). Press **Save workspace**.
4. **Overview:** press **Refresh from Ledger** (read-only test of your keys), then **Start trading session**
   near your coin's buy/sell times (table further down). Sessions are max 10 minutes - keep **Unlimited** OFF.

# TradeSentry — Streamlit dashboard

A local dashboard for the existing Trading Agent and AI Guardian. The original project is preserved separately.

## Open the dashboard

On macOS, double-click **Start Dashboard.command**. On Windows, use **Start Dashboard.bat**. The first launch installs the dashboard libraries and requires internet access. Python 3.10 or newer is recommended.

Or run from this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-gui.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

Open http://127.0.0.1:8501. This is a local application; do not publish it publicly with your credentials.

## Screens

- **Overview:** saved portfolio observations, recent decisions, local limits, read-only account refresh, and explicit session start/stop controls.
- **Live flow:** animated concurrent-agent diagram, live operation tracking, play/pause/step/scrub history replay, clickable explanations, API glossary, and call-trace CSV exports.
- **Guardian:** decision counts, filters, risk scores, full review explanations, and lockdown status.
- **Activity:** sessions with actual elapsed durations, review/event CSV exports, and the latest fetched account transactions.
- **Setup:** member, currency, theory version, key label, model, and masked credential fields. Blank key fields preserve saved credentials. Save matching settings before starting.

Opening or browsing the GUI does not call Ledger or your model. **Refresh from Ledger** makes read-only requests. **Start trading session** starts both agents and may propose/review simulated trades and incur model charges. The parent runner stops both agents at the requested 1–10 minute limit, also stopping the trader if its reviewer exits. Closing a browser tab does not stop a session; use **Stop both agents**, or let the bounded session finish. Do not run the older command-line launchers simultaneously.

The team Muse key is already in `.env`. Add your own two Ledger keys in Setup (or in `.env`). Each agent process receives only its own Ledger role key. Never share the private `.env` file. This copy starts clean (no logs, no saved state, no settings). Use it for ONE member and ONE coin only.

## Evidence and limits

The initial dashboard shows real **historical local logs**, not invented demo trades or a live connection. Portfolio observations are timestamped in UAE time. Lines join recorded samples and are not continuous monitoring. The local execution count is not the complete account history. Read-only refresh retrieves at most 200 account transactions. Existing session records are preserved, including the old session whose elapsed duration exceeded ten minutes.

This GUI does not fix the burst-detection or missing-market-evidence weaknesses identified in the code review. Those remain separate Guardian work. Account changes and human approvals are performed on Ledger itself.

## Checks

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s gui_tests -v
```

The first command runs the original agent tests plus new offline dashboard/controller checks. The second checks all four Streamlit screens, filtering, setup validation, and idle controls. No test places live trades or calls a model provider.

GUI and session-control additions were drafted with OpenAI Codex. Include this in your course AI-use disclosure.

---

# TradeSentry — COSC 435 Trading Agent + independent AI Guardian for Ledger

## TEAM QUICKSTART (each member, about 15 minutes)

You need: Python 3.9+ (Windows: install from python.org and use `py` instead of `python3`), and your own Ledger
account. For the original command-line agents, Python is sufficient. Add your own credentials to `.env`; none are included.

1. **Ledger website** (your own account):
   - *Theory Submission*: paste your coin's theory from `docs/Ledger_theories_all_coins.pdf` (rewrite the hypothesis in
     your own words), choose **Mean reversion**, tick **only your coin**, enter the five numbers, **Submit and activate**.
     Note the version number.
   - *Security Policy*: allowed currency = your coin only; max transaction $9,500; 6 trades/hour; 30/day;
     escalation threshold $20,000; restricted action WITHDRAW. **Save policy**.
   - *API Keys*: create one **trader** key and one **guardian** key (never "full"). Copy each when shown (once only).
2. **Put your keys in `.env`** (the file named `.env` in this folder; on a Mac press Cmd+Shift+. in Finder to see it,
   or run `open -e .env`; on Windows `notepad .env`). Replace the two `PASTE_...` values with your own trader and
   guardian keys and save. Set `MODEL_API_KEY` to your authorized model provider key.
   Then set up and test:
   ```
   python3 setup.py                  # your name, coin, theory version, key label (press Enter at the key prompts)
   python3 tools/llm_check.py        # must end with: LLM CHECK PASSED   (nothing is sent to Ledger)
   ```
3. **Trade in sessions of at most 10 minutes** (course rule), near your coin's daily low (buys) and high (sells):

   | Coin | Daily low (buy zone) UTC / UAE | Daily high (sell zone) UTC / UAE |
   |---|---|---|
   | BTC | 09:13 / 13:13 | 21:13 / 01:13 |
   | ETH | 06:32 / 10:32 | 18:32 / 22:32 |
   | DOGE | 03:52 / 07:52 | 15:52 / 19:52 |
   | XMR | 01:12 / 05:12 | 13:12 / 17:12 |
   | XRP | 22:31 / 02:31 | 10:31 / 14:31 |
   | SOL | 19:51 / 23:51 | 07:51 / 11:51 |

   ```
   python3 run_session.py            # one 10-minute session (Windows/macOS/Linux); Ctrl+C stops it
   bash start.sh                     # macOS/Linux alternative: same session in the background
   ```
   Run 1–2 sessions in your buy zone and 1–2 in your sell zone (about 0–2 h either side of the times above).
4. **Results for the report:**
   ```
   python3 tools/session_report.py   # writes reports/<COIN>_<name>.md + fills CSV (return, drawdown, Guardian decisions)
   ```
   Send `reports/…md`, `logs/sessions.csv` and screenshots of your Transactions and Guardian Log pages to the report writer.
   **Never** share your `.env`, your Ledger keys, or screenshots that show keys. Do not push `.env` to GitHub (it is git-ignored).


Two separate programs, each with **its own role-scoped Ledger key** and a **required language model**
(Meta Muse Spark by default; any OpenAI-compatible or Anthropic model also works):

| Program | Ledger key | What it does |
|---|---|---|
| `trading_agent/` | `trader` | Validates the submitted theory, replays prices from Ledger's public model, checks the live quote, computes the theory signal and **hard size bounds**, asks the **model to decide** inside those bounds, validates the answer, and proposes at most one order per cycle. It cannot review orders. |
| `guardian/` | `guardian` | Reviews pending orders two at a time: gathers its own evidence, applies **hard rules**, asks the **model for a judgment** (strict schema), takes the stricter of the two, **rechecks** with a fresh policy and quote, and submits exactly one review (APPROVE / MODIFY / BLOCK / ESCALATE). It cannot propose orders. |

Standard library only — **Python 3.9+**, nothing to install. Follows the *Student Development Guide*
(sections 6.2 client, 6.3 validation, 6.4 trader cycle, 6.5 sizing, 6.6 model use, 6.7 Guardian, 6.8 main loop).

```
common/          ledger_client.py (HTTPS-only, no redirects, 1.1 s spacing, Cooldown/UncertainWrite),
                 money.py (Decimal cent rounding, larger-of amounts), market_model.py (price replay),
                 llm.py (model adapter, JSON-schema output), util.py
trading_agent/   agent.py (cycle), model_decision.py (prompt, TRADER_SCHEMA, validation), strategy.py,
                 theory_check.py, order_checks.py (single validator), replay.py, config.example.json
guardian/        guardian.py (sweep, final recheck, recovery), rules.py (hard rules, anomaly, trust, risk),
                 judgment.py (prompt, GUARDIAN_SCHEMA, validation), config.example.json
data/            market_model.json, theory_v1.json (snapshots for offline replay/tests)
tests/           fake_ledger.py (in-memory API), test_system.py (42 tests)
tools/           configure.py, llm_check.py, session_report.py, scenario_demo.py
setup.py, run_session.py   one-time member setup; one <=10-minute session (any OS)
```

## 1. Configure (placeholders only in the repo; secrets only in environment variables)

```bash
python3 tools/configure.py --symbol SOL --theory-version 1 --trader-label sol-trader
#   another member: --symbol BTC --theory-version <yours> --trader-label "<your trader key label>"
export LEDGER_TRADER_KEY='...'     # trader-scoped key  (Ledger -> API Keys)
export LEDGER_GUARDIAN_KEY='...'   # guardian-scoped key, a DIFFERENT key
export MODEL_API_KEY='...'         # Meta Model API key (dev.meta.ai) for Muse Spark
python3 tools/llm_check.py         # must print "LLM CHECK PASSED" (offline; nothing sent to Ledger)
```
Other providers: `tools/configure.py ... --provider openai --model <id> --api-key-env OPENAI_API_KEY`.
`config.json`, `state/` and `logs/` are git-ignored; keys are kept in the private `.env` file and must not be included in a submission.

## 2. Run one session (course rule: at most 10 minutes)

```bash
bash start.sh            # Guardian first, then Trading Agent; both stop by themselves after 10 min
bash status.sh           # running? latest decisions? errors?
bash stop.sh             # stop early
```
Each session is recorded in `logs/sessions.csv`; audit trails in `logs/guardian.jsonl`, `logs/trading_agent.jsonl`.
Single passes: `python3 -m trading_agent.agent --config trading_agent/config.json --once --dry-run`,
`python3 -m guardian.guardian --config guardian/config.json --once`.
Human controls: `--reset-halt` (agent: after a halt or an unresolved uncertain write), `--clear-lockdown` (Guardian).

## 3. Price replay / theory validation, tests, offline scenarios

```bash
python3 -m trading_agent.replay --model-file data/market_model.json --theory-file data/theory_v1.json \
        --start 2026-11-15T12:00 --end 2026-11-22T12:00
python3 -m unittest discover -s tests -v        # includes the guide's BTC check values
python3 tools/scenario_demo.py                  # six attack scenarios through the rule layer
```

## How the model is used (and why it cannot do damage)
- **Trader:** code computes the signal (24 h mean deviation), the per-buy budget (`min(cash × fraction, cap)`,
  cents down, quantity 10 dp down) and the sell bound; the model returns `{symbol, action, quantity, reasoning}`
  (JSON schema). The answer is rejected — and the agent HOLDs — if a field is missing/extra, the quantity is not a
  positive finite number within the bound, the action differs from the theory's recommendation, HOLD has a
  quantity, or the reasoning contains instruction-like text. **Model unavailable → HOLD.**
- **Guardian:** hard rules run first; if they BLOCK, the model is not asked. Otherwise the model sees the order
  (agent reasoning marked untrusted), independently gathered evidence and the rule findings, and returns
  `{decision, reason, risk_score, modified_quantity, rules_triggered}`. It is validated strictly (risk 0–100 with
  ≤ 2 decimals, MODIFY only a decrease, null quantity otherwise, rule names ≤ 255 bytes). The final decision is
  the **stricter** of rules and model. **Model unavailable or invalid → ESCALATE.** Model-only blocks never
  trigger lockdown.
- News, theory text and agent reasoning are passed to models only as sanitised, labelled untrusted data.

## Recovery
- A proposal whose response is lost raises `UncertainWrite`: the agent stores the intent **before** sending,
  never resends, finds the order in history on the next cycle, and otherwise HOLDs until a human checks.
- A lost review response is reconciled from history on the next sweep (Ledger never executes twice).
- HTTP 429/503 → cooldown with jitter; the Guardian leaves orders pending while Ledger is unavailable.
- Any per-order error in the Guardian → ESCALATE (fail closed). Three rule blocks of the agent's key in 30 min →
  lockdown (everything escalated) until `--clear-lockdown`.

## AI use disclosure
Code drafted with Claude (Anthropic) at the team's request and reviewed/tested by the team; see the report.

## Animated flow and observability

Open **Live flow** in the sidebar. **Live now** monitors sessions started from this dashboard and reads local logs every two seconds; it does not poll Ledger. **Replay saved history** animates recorded events without invoking a model or submitting any orders. Playback compresses quiet periods; its animation speed is not a measurement of latency.

New sessions record millisecond timestamps, a shared run ID, separate process/role identities, paired API/model start and end events, endpoint names, call durations, safe response summaries, rule findings, sizing evidence, and returned decision explanations. Durations include client pacing, retry waits, and parsing. Requests with no completion become unknown after three minutes or when a session stops. A killed process can leave an incomplete span; that does not establish whether a POST executed. Agent recovery remains responsible for reconciliation.

Two unmatched start events in different lanes identify observed concurrent operations. Local stage transitions are instantaneous observations, not measured processing spans. The original logs lack request start/end timing, so their replay shows only recorded decisions and transitions; it does not manufacture API timing or concurrency. Diagram model names in replay come from the selected session. Model answers remain explicitly unvalidated until later local checks. Ledger outcomes remain separate from Guardian approval.

API telemetry excludes authentication headers, keys, full prompts, reasoning request bodies, and raw provider replies. The inspector shows model-returned rationales and deterministic findings, not private model reasoning. The flow does not change the trading strategy or Guardian's decision rules.

## Visual identity

TradeSentry uses a Midnight/Slate dark interface, Ice White text, and restrained Emerald accents.
The vector wordmark sets Trade in #F4F7FB and Sentry in #38E5AD; the shield/chart icon doubles as the favicon.
Inter is used for interface text and JetBrains Mono for figures and technical details, with local fallbacks
if Google Fonts is unavailable. `gui/theme.py` supplies shared CSS/chart tokens; `.streamlit/config.toml`
mirrors the base colors for native widgets. Guardian outcomes retain both text labels and distinct colors.
Codex assisted with this presentation-only rebrand. Trading and security behavior are unchanged.

Setup includes a reminder to rotate Ledger and model-provider API keys at least every two weeks.
Stop sessions, replace keys with the same scopes, update and verify access, then revoke the old keys.
This reminder does not track key age, expire credentials, or change authentication behavior.
