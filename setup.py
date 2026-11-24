"""One-time setup for a team member (Windows, macOS or Linux):  python3 setup.py   (Windows: py setup.py)

Asks for your name, coin, theory version and key label, then writes
  trading_agent/config.json, guardian/config.json   (no secrets)
Your two Ledger keys go in the .env file (edit it yourself, or paste them here - typed hidden).
The team Meta Muse key is already in .env, so everyone uses the same model key.
Run it again any time to change something.
"""
from __future__ import annotations

import getpass
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from common.presets import COIN_THEORY, COIN_WINDOWS_UTC, PROVIDERS  # noqa: E402
from configure import write_configs                                    # noqa: E402


def ask(prompt: str, default: str = "", choices=None) -> str:
    while True:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or default
        if v and (not choices or v in choices):
            return v
        print(f"  please enter one of: {', '.join(choices)}" if choices else "  required")


def ask_secret(prompt: str, existing: str = "") -> str:
    while True:
        v = getpass.getpass(f"{prompt}{' [Enter = keep current]' if existing else ''}: ").strip()
        if not v and existing:
            return existing
        if v and not v.startswith(("PASTE", "PLACEHOLDER")) and " " not in v:
            return v
        print("  paste the complete key (input is hidden)")


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if v and not v.upper().startswith("PASTE"):
                    out[k.strip()] = v
    return out


def update_env(path: Path, values: dict) -> None:
    """Replace KEY=... lines in .env in place (comments and other keys kept); append missing keys."""
    lines = path.read_text().splitlines() if path.exists() else ["# keys - never commit, never share outside the team"]
    seen = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if k in values:
            lines[i] = f"{k}={values[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def main():
    print(__doc__.split("\n\n")[0], "\n")
    env_path = ROOT / ".env"
    old = read_env(env_path)
    for k, v in read_env(ROOT / "private.env").items():   # older installs
        old.setdefault(k, v)
    cfg_old = {}
    if (ROOT / "trading_agent" / "config.json").exists():
        cfg_old = json.loads((ROOT / "trading_agent" / "config.json").read_text())
    member = ask("Your name (for the report)", cfg_old.get("member", ""))
    symbol = ask("Your assigned coin (BTC/ETH/DOGE/XMR/XRP/SOL)", cfg_old.get("symbol", ""), list(COIN_THEORY)).upper()
    version = int(ask("Your submitted theory version on Ledger (Theory Submission page)",
                      str(cfg_old.get("expected_theory", {}).get("version", 1))))
    print(f"  Team preset for {symbol}: {COIN_THEORY[symbol]}")
    pin = ask("Did you submit exactly these numbers? (y/n)", "y", ["y", "n"]) == "y"
    label = ask("Label of your TRADER key on Ledger's API Keys page", f"{symbol.lower()}-trader")
    provider, model, key_env = "meta", PROVIDERS["meta"][0], PROVIDERS["meta"][1]
    if not old.get(key_env):
        print("  The team Muse key is missing from .env (MODEL_API_KEY). Ask the team lead for it.")
    print("\nLedger keys: paste them now (hidden), or press Enter to keep what is already in .env.")
    trader = ask_secret("Ledger TRADER-scoped key", old.get("LEDGER_TRADER_KEY", ""))
    guardian = ask_secret("Ledger GUARDIAN-scoped key (must be a different key)", old.get("LEDGER_GUARDIAN_KEY", ""))
    if trader == guardian:
        sys.exit("The trader and guardian keys must be different keys (separate scopes).")
    vals = {"LEDGER_TRADER_KEY": trader, "LEDGER_GUARDIAN_KEY": guardian}
    if not old.get(key_env):
        vals[key_env] = ask_secret("Team Meta Muse key (MODEL_API_KEY)")
    r = write_configs(symbol, version, label, provider, model, key_env, pin, member)
    update_env(env_path, vals)
    low, high = COIN_WINDOWS_UTC[symbol]
    print(f"\nDone: {r['symbol']} with {r['model']}. Keys saved in .env (not printed).")
    print(f"Your coin's daily low ~{low} UTC (buy zone) and high ~{high} UTC (sell zone); run sessions near them.")
    print("Next:\n  python3 tools/llm_check.py        (must print LLM CHECK PASSED)\n"
          "  python3 run_session.py            (one 10-minute session; Ctrl+C stops it)\n"
          "  python3 tools/session_report.py   (your results for the report)")


if __name__ == "__main__":
    main()
