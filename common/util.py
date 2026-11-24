"""Shared helpers: config loading, audit logging, time parsing, untrusted-text scanning."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path


def load_config(path: str | Path, key_env: str) -> dict:
    """Load JSON config; the API key comes from the environment, never from the file."""
    cfg = json.loads(Path(path).read_text())
    cfg["api_key"] = os.environ.get(key_env, cfg.get("api_key", ""))
    return cfg


class AuditLog:
    """Append-only JSON-lines log, mirrored to stdout."""

    def __init__(self, path: str | Path, quiet: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.quiet = quiet

    def __call__(self, event: str, **fields):
        rec = {"ts": now_utc().isoformat(timespec="milliseconds"), "event": event,
               "role": self.path.stem, "process_id": os.getpid(),
               "run_id": os.environ.get("LEDGER_RUN_ID", ""), **fields}
        line = json.dumps(rec, default=str)
        with self.path.open("a") as fh:
            fh.write(line + "\n")
        if not self.quiet:
            print(line, file=sys.stdout, flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc(s: str) -> float:
    """Ledger timestamps look like '2026-11-22 12:07:40' (UTC)."""
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()


# Patterns typical of prompt injection or attempts to steer an agent through data.
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (the |your |all )?(rules|policy|instructions|guardian)",
    r"\b(system|developer|assistant)\s*(prompt|message|:)",
    r"you are now|act as|new instructions|override",
    r"\b(approve|execute|auto-?approve)\b.{0,40}\b(trade|order|transaction|all)",
    r"\b(withdraw|transfer|send)\b.{0,40}\b(funds?|balance|wallet|address|usd|btc|sol)",
    r"\b(buy|sell|purchase)\b.{0,30}(\ball (of )?(your|the|my)\b|\beverything\b|\bmax(imum)? possible\b|\bas much as\b|\b100 ?%)",
    r"\b(skip|bypass|disable|circumvent)\b.{0,25}\b(guardian|review|checks?|limits?|polic(y|ies)|safety)",
    r"api[_ -]?key|password|secret|private key|seed phrase",
    r"https?://|0x[0-9a-f]{20,}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}",
    r"[A-Za-z0-9+/]{60,}={0,2}",  # long base64 blobs
    r"</?(system|instructions?|prompt|tool)>",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


def injection_hits(text: str) -> list[str]:
    """Return the patterns that match; empty list means no obvious injection."""
    text = text or ""
    return [p.pattern for p in _INJ if p.search(text)]


def sanitize_for_prompt(text: str, limit: int = 600) -> str:
    """Neutralise untrusted text before it is shown to an LLM: strip control chars and
    tag-like markup, collapse whitespace, truncate."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text or "")
    text = re.sub(r"<[^>]{0,40}>", "[tag]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


ENV_FILES = (".env", "private.env")   # .env is the team file; private.env kept for older installs


def load_private_env(root, role: str | None = None) -> list[str]:
    """Read KEY=VALUE lines from the git-ignored .env next to the code (guide 3.4: keys live in a private
    local configuration file read as data). Existing environment variables win; empty values and
    PASTE_... placeholders are ignored. Returns the names loaded (never the values)."""
    loaded = []
    for name in ENV_FILES:
        p = Path(root) / name
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.split(" #")[0].strip().strip("'\"")
            if role and k == ("LEDGER_GUARDIAN_KEY" if role == "trader" else "LEDGER_TRADER_KEY"):
                continue
            if k and v and not v.upper().startswith("PASTE") and not os.environ.get(k):
                os.environ[k] = v
                loaded.append(k)
    return loaded
