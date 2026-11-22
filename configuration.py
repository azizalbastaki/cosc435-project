"""Private JSON configuration; callers receive only the fields they require."""

import json

CONFIG_LIMIT = 8192
LEDGER_FIELDS = {"LEDGER_BASE_URL", "TRADER_API_KEY"}
GUARDIAN_FIELDS = {"LEDGER_BASE_URL", "GUARDIAN_API_KEY"}
MUSE_FIELDS = {"LLM_PROVIDER", "META_BASE_URL", "MUSE_MODEL", "META_API_KEY"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def read_config(path, *, role="trader"):
    if role not in {"trader", "guardian"}:
        raise ValueError("Choose trader or guardian role")
    if role == "trader":
        allowed_fields = LEDGER_FIELDS | MUSE_FIELDS
    else:
        allowed_fields = GUARDIAN_FIELDS | MUSE_FIELDS
    try:
        with open(path, "rb") as handle:
            raw = handle.read(CONFIG_LIMIT + 1)
        if len(raw) > CONFIG_LIMIT:
            raise ValueError
        data = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(data, dict) or not data:
            raise ValueError
        if not set(data).issubset(allowed_fields):
            raise ValueError
        for value in data.values():
            if not isinstance(value, str) or not value.strip():
                raise ValueError
        return data
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Cannot load config: use known nonempty string fields (max 8 KiB)") from None


def ledger_config(path):
    data = read_config(path)
    if not LEDGER_FIELDS <= data.keys():
        raise ValueError("Configure LEDGER_BASE_URL and TRADER_API_KEY")
    return {field: data[field] for field in LEDGER_FIELDS}


def guardian_config(path):
    data = read_config(path, role="guardian")
    if not GUARDIAN_FIELDS <= data.keys():
        raise ValueError("Configure LEDGER_BASE_URL and GUARDIAN_API_KEY in a separate role config")
    return {field: data[field] for field in GUARDIAN_FIELDS}
