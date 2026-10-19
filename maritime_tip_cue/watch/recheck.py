import argparse
import hashlib
import sys
from collections import Counter

from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry
from maritime_tip_cue.watch.run import stage_two_s2
from maritime_tip_cue.watch.state import Store


def recheck(no: int) -> dict:
    path = registry.folder_of(no)
    rules = rules_mod.load()
    store = Store(path / "state.db", path)
    before, after = Counter(), Counter()
    try:
        for sc in store.scenes():
            if sc["source"] != "s2" or sc["status"] not in ("detected", "checked") or not sc["data"]:
                continue
            for h in store.hulls(sc["scene_id"], sc["site"]):
                before[h["verdict"]] += 1
            scene = {"scene": sc["scene_id"], "site": sc["site"], "source": sc["source"], "time": sc["capture_utc"]}
            stage_two_s2(store, scene, path / sc["folder"], rules, sc["data"])
            for h in store.hulls(sc["scene_id"], sc["site"]):
                after[h["verdict"]] += 1
        sha = hashlib.sha1(rules_mod.RULES_PATH.read_bytes()).hexdigest()
        store.event(None, "info", None, f"S2 pair check re-run with rules {sha[:8]}: {dict(before)} -> {dict(after)}")
    finally:
        store.close()
    return {"before": dict(before), "after": dict(after)}


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.watch.recheck")
    parser.add_argument("runs", nargs="+", type=int)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    for no in args.runs:
        r = recheck(no)
        print(f"run {no}: S2 {r['before']} -> {r['after']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
