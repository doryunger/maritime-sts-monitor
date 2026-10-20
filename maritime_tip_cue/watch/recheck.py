import argparse
import hashlib
import sys
from collections import Counter

from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry, s1
from maritime_tip_cue.watch.run import stage_two_s1, stage_two_s2
from maritime_tip_cue.watch.state import Store


def recheck(no: int, sources: tuple[str, ...] = ("s1", "s2")) -> dict:
    path = registry.folder_of(no)
    rules = rules_mod.load()
    store = Store(path / "state.db", path)
    before, after = Counter(), Counter()
    try:
        scenes = [sc for sc in store.scenes() if sc["source"] in sources and sc["status"] in ("detected", "checked") and sc["data"]]
        for sc in scenes:
            for h in store.hulls(sc["scene_id"], sc["site"]):
                before[(sc["source"], h["verdict"])] += 1
        passes: dict[str, list] = {}
        for sc in scenes:
            scene = {"scene": sc["scene_id"], "site": sc["site"], "source": sc["source"], "time": sc["capture_utc"]}
            if sc["source"] == "s2":
                stage_two_s2(store, scene, path / sc["folder"], rules, sc["data"])
            else:
                passes.setdefault(s1.pass_of(sc["scene_id"]), []).append((scene, path / sc["folder"], sc["data"]["objects"]))
        for pass_id, members in passes.items():
            stage_two_s1(store, pass_id, members, rules)
        for sc in scenes:
            for h in store.hulls(sc["scene_id"], sc["site"]):
                after[(sc["source"], h["verdict"])] += 1
        sha = hashlib.sha1(rules_mod.RULES_PATH.read_bytes()).hexdigest()
        store.event(None, "info", None, f"pair check re-run ({', '.join(sources)}) with rules {sha[:8]}")
    finally:
        store.close()
    return {"before": dict(before), "after": dict(after)}


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor recheck")
    parser.add_argument("runs", nargs="+", type=int)
    parser.add_argument("--sources", default="s1,s2")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    for no in args.runs:
        r = recheck(no, tuple(args.sources.split(",")))
        for src in sorted({k[0] for k in r["before"]}):
            b = {k[1]: v for k, v in r["before"].items() if k[0] == src}
            a = {k[1]: v for k, v in r["after"].items() if k[0] == src}
            print(f"run {no} {src}: {b} -> {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
