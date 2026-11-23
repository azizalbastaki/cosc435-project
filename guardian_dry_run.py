"""Review one real pending order locally. Never submit a Guardian decision."""

import argparse
import math
import sys

from client import ApiError, Cooldown, LedgerClient
from configuration import guardian_config
from guardian import review_next
from guardian_live import LiveGuardianSource
from model_adapter import MuseAdapter


class LazyGuardianModel:
    """Do not require or charge Muse when the queue is empty or hard rules fail."""
    def __init__(self, config_path):
        self.config_path = config_path
        self.adapter = None

    def generate(self, prompt, payload, schema):
        if self.adapter is None:
            self.adapter = MuseAdapter.from_config(self.config_path, role="guardian")
        return self.adapter.generate(prompt, payload, schema)

    def close(self):
        if self.adapter is not None:
            self.adapter.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.guardian.json")
    args = parser.parse_args(argv)
    api = None
    model = LazyGuardianModel(args.config)
    try:
        config = guardian_config(args.config)
        api = LedgerClient(config["LEDGER_BASE_URL"], config["GUARDIAN_API_KEY"], role="guardian")
        source = LiveGuardianSource(api)
        result = review_next(source, model)
        print(f"Guardian dry run: {result['decision']}")
        if source.order is not None:
            print(f"Order #{source.order['transaction_id']} | theory {source.order['theory_id']}/v{source.order['theory_version']}")
        if result["quantity"] is not None:
            print(f"Locally checked quantity: {result['quantity']:f}")
        print(result["reason"])  # Internal fixed messages, never raw model/server text.
        print("Local assessment only. No review submitted; pending orders are unchanged.")
        return 0
    except Cooldown as exc:
        print(f"Wait at least {math.ceil(exc.seconds)} seconds. Order left pending; no review submitted.", file=sys.stderr)
        return 2
    except (ValueError, ApiError):
        print("Guardian configuration or read failed. No review submitted.", file=sys.stderr)
        return 1
    finally:
        model.close()
        if api is not None:
            api.close()


if __name__ == "__main__":
    sys.exit(main())
