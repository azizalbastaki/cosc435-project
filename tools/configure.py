"""Write the private config.json files for one team member (no secrets are written here).

  python3 tools/configure.py --symbol SOL --theory-version 1 --trader-label sol-trader
  python3 tools/configure.py --symbol BTC --theory-version 1 --trader-label btc-trader --provider gemini
  python3 tools/configure.py ... --no-pin      # your submitted numbers differ from the team presets

Prefer `python3 setup.py`, which asks the questions and also fills in .env.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.presets import COIN_THEORY, PROVIDERS  # noqa: E402


def write_configs(symbol: str, theory_version: int, trader_label: str, provider: str = "meta",
                  model: str | None = None, api_key_env: str | None = None, pin: bool = True,
                  member: str = "", strategy: str = "mean_reversion", params: dict | None = None) -> dict:
    if strategy not in ("mean_reversion", "momentum"):
        raise ValueError("unsupported strategy")
    if strategy == "momentum" and pin and params is None:
        raise ValueError("No researched momentum preset: use --no-pin, then pin your replay-validated params")
    if params is not None:
        from trading_agent.theory_check import validate_theory, TheoryInvalid
        expected = set(COIN_THEORY["SOL"])
        if not isinstance(params, dict) or set(params) != expected:
            raise ValueError("Provide all five validated numeric theory parameters.")
        try:
            validate_theory(dict(params, id=1, version=theory_version, status="SUBMITTED",
                                 selected_symbols=[symbol], strategy=strategy),
                            {"symbol": symbol, "expected_theory": {}})
        except TheoryInvalid as exc:
            raise ValueError(str(exc)) from None
    if symbol not in COIN_THEORY:
        raise ValueError(f"unsupported symbol {symbol}")
    dmodel, denv, _ = PROVIDERS[provider]
    model = model or dmodel
    if not model:
        raise ValueError(f"--model is required for provider {provider}")
    if model.endswith("-contributor"):
        raise ValueError("Refusing *-contributor models (inputs may be used for training).")
    env = api_key_env or denv
    for name in ("trading_agent", "guardian"):
        d = json.loads((ROOT / name / "config.example.json").read_text())
        d["model_adapter"].update(provider=provider, model=model, api_key_env=env)
        d["model_adapter"].pop("base_url", None)
        if name == "trading_agent":
            d["symbol"] = symbol
            d["member"] = member
            d["expected_theory"]["version"] = theory_version
            d["expected_theory"]["strategy"] = strategy
            d["expected_theory"]["params"] = dict(params if params is not None else COIN_THEORY[symbol]) if pin else {}
        else:
            d["allowed_symbols"] = [symbol]
            d["expected_trader_key_labels"] = [trader_label]
        (ROOT / name / "config.json").write_text(json.dumps(d, indent=2))
    return {"symbol": symbol, "model": f"{provider}:{model}", "key_env": env, "pinned": pin}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True, choices=sorted(COIN_THEORY))
    ap.add_argument("--theory-version", type=int, required=True)
    ap.add_argument("--trader-label", required=True, help="label of YOUR trader key on Ledger's API Keys page")
    ap.add_argument("--member", default="", help="your name (used in reports)")
    ap.add_argument("--strategy", choices=("mean_reversion", "momentum"), default="mean_reversion")
    ap.add_argument("--no-pin", action="store_true")
    ap.add_argument("--provider", default="meta", choices=sorted(PROVIDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default=None)
    a = ap.parse_args()
    try:
        r = write_configs(a.symbol, a.theory_version, a.trader_label, a.provider, a.model, a.api_key_env,
                          not a.no_pin, a.member, a.strategy)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"wrote trading_agent/config.json and guardian/config.json for {r['symbol']}; model {r['model']} "
          f"(key from ${r['key_env']}); settings {'pinned to team preset' if r['pinned'] else 'not pinned'}.")
    print("Next: python3 tools/llm_check.py")


if __name__ == "__main__":
    main()
