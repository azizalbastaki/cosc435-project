"""Explicit one-call Muse access check with synthetic data; no Ledger operations."""

import argparse
import sys

from model_adapter import ModelError, MuseAdapter

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    adapter = None
    try:
        adapter = MuseAdapter.from_config(args.config)
        adapter.generate(
            'This is a connection test. Return only {"status":"ok"}.',
            {"task": "connection_check"}, SCHEMA,
        )
        print("Muse connection check OK: structured response validated. No Ledger requests made.")
        return 0
    except (ValueError, ModelError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if adapter is not None:
            adapter.close()


if __name__ == "__main__":
    sys.exit(main())
