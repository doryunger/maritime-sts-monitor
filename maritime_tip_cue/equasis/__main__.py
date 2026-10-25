import argparse
import json
import sys

from maritime_tip_cue.equasis import resolve
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch.state import Store


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor equasis")
    parser.add_argument("runs", nargs="+", type=int)
    args = parser.parse_args()
    rules = rules_mod.load()["identity"]
    for no in args.runs:
        store = Store.open(no)
        try:
            print(json.dumps({"run": no, **resolve.enrich(store, rules)}, sort_keys=True), flush=True)
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
