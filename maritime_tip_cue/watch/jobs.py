import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import shape

from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import run as runmod
from maritime_tip_cue.watch.state import Store


def execute(manifest: dict, store: Store, rules_path: Path) -> dict:
    from maritime_tip_cue.ais import match

    rules = rules_mod.load(rules_path)
    site = manifest["site"]
    scenes = [row | {"geometry": shape(row["geometry"])} for row in manifest["scenes"]]
    times = [datetime.fromisoformat(row["time"].replace("Z", "+00:00")).astimezone(UTC) for row in scenes]
    start, end = min(times), max(times)
    sources = ",".join(sorted({row["source"] for row in scenes}))
    args = argparse.Namespace(sources=sources, reanalyse=False)
    out = store.path
    run_id = store.start_run(
        {
            "job_id": manifest["job_id"],
            "run_no": store.run_no,
            "rules_sha256": manifest["rules_sha256"],
            "hot_spot_settings": site,
        },
        site["name"],
        (start.isoformat(), end.isoformat()),
        sources,
        hashlib.sha1(rules_path.read_bytes()).hexdigest(),
        runmod.git_head(),
    )
    runmod.SINK.update(store=store, run_id=run_id)
    status = "failed"
    try:
        failed = runmod.run(store, run_id, args, rules, site, start, end, out, found=scenes)
        rows = [store.scene(row["scene"], row["site"]) for row in scenes]
        counts = {"checked": sum(row is not None and row["status"] == "checked" for row in rows)}
        counts["failed"] = len(scenes) - counts["checked"]
        if failed or counts["failed"]:
            raise RuntimeError(f"job has {counts['failed']} incomplete scenes")
        match.run_match(store, match.select_hulls(store, match.PAIR_VERDICTS, False, False), rules, runmod.log)
        status = "done"
        return counts
    finally:
        runmod.SINK.update(store=None, run_id=None, scene=None)
        store.finish_run(run_id, status)
