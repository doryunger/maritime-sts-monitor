import argparse
import hashlib
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maritime_tip_cue import structures as structures_mod, hotspots, passes
from maritime_tip_cue.config import ROOT, settings
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import common, detector, registry, review, s1, s2
from maritime_tip_cue.watch.geo import Shore, Structures
from maritime_tip_cue.watch.state import VERDICTS, Store

SINK: dict = {"store": None, "run_id": None, "scene": None}


def log(text: str, level: str = "info") -> None:
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)
    if SINK["store"] is not None:
        SINK["store"].event(SINK["run_id"], level, SINK["scene"], text)


def scene_folder(out: Path, scene: dict) -> Path:
    return out / "scenes" / scene["source"] / scene["scene"]


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def work_dir(folder: Path, scene: dict) -> Path:
    root = settings()["ai2"].get("scratch_root")
    if not root:
        return folder / "work"
    return Path(root) / hashlib.sha1(scene["scene"].encode()).hexdigest()[:10]


def stage_one(store: Store, scene: dict, site: dict, shore: Shore, rules: dict, folder: Path, structures: Structures | None = None) -> dict:
    source = scene["source"]
    need = rules["detectors"][f"min_free_gb_{source}"]
    if free_gb(folder) < need:
        raise RuntimeError(f"free disk {free_gb(folder):.1f} GB is below {need} GB")
    work = work_dir(folder, scene)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        secs, windows, features = detector.detect(source, scene, rules, work, tuple(site["bbox"]), log)
        dets, counts = common.prepare(features, source, site, shore, rules, structures)
        scratch = work / "scratch"
        if source == "s2":
            cands = s2.candidates(dets, rules)
            index = s2.extract(scratch, cands, scene, rules, folder / "s2_windows", log) if cands else []
            data = {"dets": dets, "index": index, "windows": windows}
            info = {"dets": counts, "candidates": len(cands)}
        else:
            wins = s1.load_windows(scratch, windows)
            pixel = rules["s1_outline"]["pixel_m"]
            placed, outside = s1.place(dets, wins, pixel)
            counts["outside_windows"] = outside
            objs = s1.group_objects(wins, placed, rules["s1_objects"], pixel)
            big = [o for o in objs if o["length_m"] >= rules["s1_objects"]["min_length_m"]]
            saved = s1.save_chips(folder / "chips", big, placed, wins, rules)
            data = {"dets": placed, "objects": saved, "windows": windows}
            info = {"dets": counts, "objects": len(objs), "candidates": len(big)}
        if not rules["detectors"]["keep_scratch"]:
            shutil.rmtree(scratch, ignore_errors=True)
            for tif in work.glob("*.tif"):
                tif.unlink()
        store.save_detected(scene, features, info, round(secs), data)
        return info | {"detector_s": round(secs)}
    finally:
        if not rules["detectors"]["keep_scratch"]:
            shutil.rmtree(work, ignore_errors=True)


def stage_two_s2(store: Store, scene: dict, folder: Path, rules: dict, data: dict) -> dict:
    rows = s2.analyse(folder / "s2_windows", data["dets"], data["index"], rules)
    hulls = [
        {"det_id": r["det_id"], "lat": r["lat"], "lon": r["lon"], "ai2_length_m": r["length_m"], "ai2_width_m": r["width_m"], "ai2_heading": r["heading"], "ai2_speed_kn": r["speed_kn"], "ai2_score": r["score"], "verdict": r["verdict"], "role": r["role"], "record": r["record"], "check": r["check"]}
        for r in rows
    ]
    return store.save_checked(scene, hulls)


def stage_two_s1(store: Store, pass_id: str, members: list[tuple[dict, Path, list]], rules: dict) -> dict[str, dict]:
    items = []
    for scene, folder, objects in members:
        for o in objects:
            items.append((scene, folder, o))
    results = s1.evaluate_pass([(folder / "chips", o) for _, folder, o in items], rules) if items else []
    out: dict[str, list] = {scene["scene"]: [] for scene, _, _ in members}
    for (scene, folder, o), r in zip(items, results):
        verdict = r["verdict"] if r["length_m"] is not None else "unmeasured"
        own = [d for d in o["detections"] if d["own"]]
        out[scene["scene"]].append(
            {
                "pass": pass_id,
                "obj_id": o["obj_id"],
                "lat": o["lat"],
                "lon": o["lon"],
                "ai2_length_m": o["ai2_max_length_m"],
                "ai2_boxes": [{k: d[k] for k in ("length_m", "width_m", "heading", "score")} for d in own],
                "object_length_m": o["length_m"],
                "object_width_m": o["width_m"],
                "verdict": verdict,
                "length_m": r["length_m"],
                "width_beams": r["width_beams"],
                "light_separation": r["light_separation"],
                "overlap": r["overlap"],
                "fill": r["fill"],
                "weak_evidence": r["weak_evidence"],
                "vessels": r["vessels"],
                "blur_m": r["blur_m"],
                "object": o,
            }
        )
    return {scene["scene"]: store.save_checked(scene, out[scene["scene"]], pass_id) for scene, _, _ in members}


def line(scene: dict, info: dict, two: dict) -> str:
    kept = info["dets"]["kept"]
    v = two["verdicts"]
    counts = " ".join(f"{k} {v.get(k, 0)}" for k in VERDICTS)
    extra = f", objects {info['objects']}" if "objects" in info else ""
    return f"{scene['source']} {scene['time'][:16]} {scene['scene'][:40]}: kept {kept}{extra}, checked {two['checked']}, {counts}, detector {info['detector_s']} s"


def git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def stored_info(row: dict) -> dict:
    return row["counts"] | {"detector_s": round(row["detector_s"])}


def run(store: Store, run_id: int, args, rules: dict, site: dict, start: datetime, end: datetime, out: Path) -> bool:
    found = [p for p in passes.find(start, end, [site], None) if p["source"] in args.sources.split(",")]
    log(f"{len(found)} scenes: " + ", ".join(f"{p['source']} {p['time'][:16]}" for p in found))
    shore = Shore(site["bbox"], site["min_shore_distance_m"])
    structures = Structures(site["bbox"], rules["fixed_structures"]["exclusion_m"], structures_mod.load())
    log(f"{len(structures.points)} charted structures near {site['name']}")
    infos: dict[str, dict] = {}
    rows: dict[str, dict] = {}
    total = 0.0
    failed = False
    for scene in found:
        folder = scene_folder(out, scene)
        folder.mkdir(parents=True, exist_ok=True)
        SINK["scene"] = scene["scene"]
        try:
            row = store.scene(scene["scene"], scene["site"])
            if row and row["status"] in ("detected", "checked"):
                info = stored_info(row)
            else:
                store.begin_scene(run_id, scene, folder, s1.pass_of(scene["scene"]) if scene["source"] == "s1" else None)
                info = stage_one(store, scene, site, shore, rules, folder, structures)
                row = store.scene(scene["scene"], scene["site"])
            total += info["detector_s"]
            infos[scene["scene"]] = info
            rows[scene["scene"]] = row
            if scene["source"] == "s2":
                if args.reanalyse or row["status"] != "checked":
                    two = stage_two_s2(store, scene, folder, rules, row["data"])
                else:
                    two = row["counts"]
                log(line(scene, info, two))
        except Exception as e:
            failed = True
            log(f"{scene['scene']}: FAILED {type(e).__name__}: {str(e)[:600]}", "error")
            store.fail(scene, f"{type(e).__name__}: {str(e)[:600]}")
            traceback.print_exc()
            shutil.rmtree(work_dir(folder, scene), ignore_errors=True)
    SINK["scene"] = None
    by_pass: dict[str, list] = {}
    for scene in found:
        if scene["source"] == "s1" and scene["scene"] in infos:
            by_pass.setdefault(s1.pass_of(scene["scene"]), []).append((scene, scene_folder(out, scene), rows[scene["scene"]]))
    for pass_id, members in by_pass.items():
        if args.reanalyse or any(r["status"] != "checked" for _, _, r in members):
            summary = stage_two_s1(store, pass_id, [(s, f, r["data"]["objects"]) for s, f, r in members], rules)
        else:
            summary = {s["scene"]: r["counts"] for s, _, r in members}
        for scene, _, _ in members:
            log(line(scene, infos[scene["scene"]], summary[scene["scene"]]))
    sheet = review.build(store, out, rules)
    log(f"detector total {total:.0f} s; review sheet {sheet}")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor run")
    parser.add_argument("--site", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--run", type=int)
    parser.add_argument("--out")
    parser.add_argument("--db")
    parser.add_argument("--sources", default="s1,s2")
    parser.add_argument("--reanalyse", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rules = rules_mod.load()
    site = next(s for s in hotspots.load() if s["name"] == args.site)
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).astimezone(timezone.utc)
    start = end - timedelta(days=args.days)
    if args.out:
        out = Path(args.out)
        out = out if out.is_absolute() else ROOT / out
        out.mkdir(parents=True, exist_ok=True)
        number = registry.register(out)
    elif args.run:
        number, out = args.run, registry.folder_of(args.run)
    else:
        number, out = registry.new_run()
    print(f"run {number}: {out}", flush=True)
    store = Store(Path(args.db) if args.db else out / "state.db", out)
    rules_sha1 = hashlib.sha1(rules_mod.RULES_PATH.read_bytes()).hexdigest()
    run_id = store.start_run(vars(args) | {"hot_spot_settings": site}, args.site, (start.isoformat(), end.isoformat()), args.sources, rules_sha1, git_head())
    SINK.update(store=store, run_id=run_id)
    status = "failed"
    try:
        status = "failed" if run(store, run_id, args, rules, site, start, end, out) else "done"
    finally:
        SINK["store"] = None
        store.finish_run(run_id, status)
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
