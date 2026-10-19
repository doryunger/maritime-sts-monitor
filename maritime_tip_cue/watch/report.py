import json
import math
import random
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from rasterio.warp import transform as warp_transform

from maritime_tip_cue.sts import fixtures, rules as rules_mod
from maritime_tip_cue.watch import review, s1 as s1mod
from maritime_tip_cue.watch.geo import Shore, haversine_m
from maritime_tip_cue.watch.state import IGNORED_ROLES, VERDICTS, Store

SCALE = 6
S1_DB_RANGE = (22.0, 50.0)
SECTIONS = ("pair", "possible_pair", "unmeasured", "moving", "single")
DATE = re.compile(r"_(\d{4}-\d{2}-\d{2})$")
S1_KEYS = ("length_m", "width_beams", "light_separation", "overlap", "fill", "blur_m")
S2_KEYS = ("width_ratio", "ratio_ab", "unmeasured_reason", "shape_step_beams", "shape_step_k", "shape_continuity", "shape_straight_k", "shape_call", "weak_evidence")
KEPT_TOL_M = 40
RAW_TOL_M = 60


def clean(value):
    return json.loads(json.dumps(value, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def run_section(store: Store, scenes: list[dict]) -> dict:
    rows = [dict(r) for r in store.db.execute("select * from runs order by run_id")]
    named = [r for r in rows if r["hot_spot"]]
    times = [s["capture_utc"] for s in scenes]
    return {
        "hot_spot": named[-1]["hot_spot"] if named else ", ".join(sorted({s["site"] for s in scenes if s["site"]})),
        "window": [rows[0]["window_start"] or min(times, default=""), rows[-1]["window_end"] or max(times, default="")] if rows else [min(times, default=""), max(times, default="")],
        "sources": (named[-1]["sources"] if named else ",".join(sorted({s["source"] for s in scenes}))),
        "rules_sha1": next((r["rules_sha1"] for r in reversed(rows) if r["rules_sha1"]), ""),
        "commit": next((r["commit_hash"] for r in reversed(rows) if r["commit_hash"]), ""),
        "runs": [{"run_id": r["run_id"], "start": r["started_utc"], "end": r["ended_utc"], "status": r["status"]} for r in rows],
    }


def scene_section(scenes: list[dict]) -> list[dict]:
    out = []
    for s in scenes:
        c = s["counts"] or {}
        d = c.get("dets", {})
        v = c.get("verdicts", {})
        out.append(
            {
                "scene_id": s["scene_id"],
                "site": s["site"],
                "source": s["source"],
                "time": s["capture_utc"],
                "status": s["status"],
                "detector_s": s["detector_s"],
                "raw": d.get("raw"),
                "kept": d.get("kept"),
                "checked": c.get("checked"),
                **{k: v.get(k, 0) for k in VERDICTS},
                "error": s["error"],
            }
        )
    return out


def hull_rows(store: Store, scene: dict) -> list[dict]:
    out = []
    for r in store.db.execute("select * from hulls where scene_id=? and site=? order by rowid", (scene["scene_id"], scene["site"])):
        rec = json.loads(r["record"])
        out.append(rec | {"hull_id": r["hull_id"], "lat": r["lat"], "lon": r["lon"], "verdict": r["verdict"], "role": r["role"]})
    return out


def describe(scene: dict, rec: dict, taken: set) -> dict:
    hid = f"{scene['scene_id']}__{rec['hull_id']}"
    if hid in taken:
        hid += f"__{scene['site']}"
    taken.add(hid)
    base = {"id": hid, "scene_id": scene["scene_id"], "site": scene["site"], "hull_id": rec["hull_id"], "source": scene["source"], "time": scene["capture_utc"], "lat": rec["lat"], "lon": rec["lon"], "verdict": rec["verdict"], "role": rec["role"]}
    if scene["source"] == "s1":
        box = (rec.get("ai2_boxes") or [{}])[0]
        base["ai2"] = {"length_m": rec.get("ai2_length_m"), "width_m": box.get("width_m"), "heading": box.get("heading"), "boxes": len(rec.get("ai2_boxes") or [])}
        base["measures"] = {k: rec.get(k) for k in S1_KEYS} | {"object_length_m": rec.get("object_length_m"), "object_width_m": rec.get("object_width_m")}
    else:
        chk = rec.get("check") or {}
        base["ai2"] = {"length_m": rec.get("ai2_length_m"), "width_m": rec.get("ai2_width_m"), "heading": rec.get("ai2_heading"), "speed_kn": rec.get("ai2_speed_kn")}
        base["measures"] = {k: chk.get(k) for k in S2_KEYS} | {k: chk.get(k) for k in ("hull_a_length_m", "hull_a_width_m", "hull_b_length_m", "hull_b_width_m")}
    return base


def s1_tile(folder: Path, obj: dict, rules: dict, overlay: bool) -> Image.Image:
    z = np.load(folder / "chips" / obj["file"])
    lo, hi = S1_DB_RANGE
    db = 10 * np.log10(np.clip(z["vh"], 1e-9, None))
    g = np.clip((db - lo) / (hi - lo), 0, 1)
    tr = z["transform"]
    h, w = g.shape
    x0, y0 = float(tr[2]), float(tr[5])
    col, rw = (obj["x"] - x0) / 10, (y0 - obj["y"]) / 10
    half = int(rules["review"]["crop_half_m"] / 10)
    r0, c0, r1, c1 = review.crop_box(col, rw, min(half, h // 2, w // 2), h, w)
    img = Image.fromarray((g[r0:r1, c0:c1] * 255).astype(np.uint8)).convert("RGB")
    img = img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
    if not overlay:
        return img
    d = ImageDraw.Draw(img)

    def px(x, y):
        return ((x - x0) / 10 - c0) * SCALE, ((y0 - y) / 10 - r0) * SCALE

    for det in obj["detections"]:
        if None in (det["length_m"], det["width_m"], det["heading"]):
            continue
        review.draw_rect(d, [px(*p) for p in review.corners(det["x"], det["y"], det["heading"], det["length_m"], det["width_m"])], (255, 255, 0) if det["own"] else (0, 255, 255), 1)
    built = s1mod.build_object(folder / "chips", obj, rules)["d"]
    if built is not None:
        t, length, width = built["rect"]
        u = np.array([math.cos(math.radians(t)), math.sin(math.radians(t))])
        n = np.array([-u[1], u[0]])
        pu, pn = built["xy"] @ u, built["xy"] @ n
        cu, cn = (pu.max() + pu.min()) / 2, (pn.max() + pn.min()) / 2
        corners = [u * (cu + a * length / 2) + n * (cn + b * width / 2) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        review.draw_rect(d, [px(*p) for p in corners], (255, 255, 255), 1)
        for a, b in (built.get("par") or {}).get("ext", []):
            d.line([px(*a), px(*b)], fill=(255, 60, 60), width=2)
    return img


def window_of(scene: dict, rec: dict) -> str | None:
    return next((w["file"] for w in (scene["data"] or {}).get("index", []) if rec["hull_id"] in w["members"] and w["file"]), None)


def render(root: Path, scene: dict, rec: dict, rules: dict, overlay: bool) -> Image.Image | None:
    folder = root / scene["folder"]
    if scene["source"] == "s2":
        window = window_of(scene, rec)
        if window is None:
            return None
        cfg = rules["review"] | {"scale": SCALE}
        return review.s2_tile(folder / "s2_windows", window, rec, cfg, overlay=overlay, ai2=overlay)
    return s1_tile(folder, rec["object"], rules, overlay)


def site_of(store: Store, scenes: list[dict]) -> dict | None:
    for r in store.db.execute("select args from runs order by run_id desc"):
        site = json.loads(r["args"] or "{}").get("hot_spot_settings")
        if site:
            return site
    name = next((s["site"] for s in scenes if s["site"]), "")
    try:
        from maritime_tip_cue import hotspots

        return next((s for s in hotspots.load() if s["name"] == name), None)
    except Exception:
        return None


class Dropped:
    def __init__(self, store: Store, scenes: list[dict], site: dict | None):
        self.site = site
        self.shore = Shore(site["bbox"], site["min_shore_distance_m"]) if site else None
        self.raw = {}
        for s in scenes:
            rows = store.db.execute("select lat, lon from raw_detections where scene_id=? and site=?", (s["scene_id"], s["site"])).fetchall()
            kept = (s["data"] or {}).get("dets", [])
            self.raw[(s["scene_id"], s["site"])] = (rows, kept)

    def explain(self, points: list[tuple[float, float]]) -> dict:
        best, kept = None, []
        for rows, scene_kept in self.raw.values():
            for lat, lon in points:
                for r in rows:
                    dist = haversine_m(lat, lon, r["lat"], r["lon"])
                    if best is None or dist < best[0]:
                        best, kept = (dist, r["lat"], r["lon"]), scene_kept
        if best is None or best[0] > RAW_TOL_M:
            return {"reason": "no_detection", "nearest_raw_m": None if best is None else round(best[0], 1)}
        near = round(best[0], 1)
        if any(haversine_m(best[1], best[2], k["lat"], k["lon"]) <= KEPT_TOL_M for k in kept):
            return {"reason": "not_checked", "nearest_raw_m": near}
        out = {"nearest_raw_m": near}
        if self.site is None:
            return out | {"reason": "dropped_by_filter"}
        w, s, e, n = self.site["bbox"]
        if not (w <= best[2] <= e and s <= best[1] <= n):
            return out | {"reason": "outside_hot_spot"}
        dist = self.shore.distance_m(best[2], best[1])
        return out | {"reason": "shore_distance", "shore_m": None if math.isinf(dist) else round(dist)}


def nearest(hulls: list[dict], lat: float, lon: float):
    best = None
    for h in hulls:
        dist = haversine_m(lat, lon, h["lat"], h["lon"])
        rank = (h["role"] in IGNORED_ROLES, dist)
        if dist <= RAW_TOL_M and (best is None or rank < best[0]):
            best = (rank, h, dist)
    return None if best is None else (best[1], best[2])


def compare(entry: dict, hull: dict | None, dist: float | None, fx_measures: dict, keys: tuple, listed: set) -> dict:
    if hull is None:
        return entry | {"status": "missing", "run_verdict": None, "distance_m": None, "hull": None, "measures": {k: [fx_measures.get(k), None] for k in keys}}
    agree = hull["verdict"] == entry["fixture_verdict"]
    return entry | {
        "status": "agree" if agree else "differ",
        "run_verdict": hull["verdict"],
        "distance_m": round(dist, 1),
        "hull": hull["id"] if hull["id"] in listed else None,
        "hull_id": hull["id"],
        "measures": {k: [fx_measures.get(k), hull["measures"].get(k)] for k in keys},
    }


def totals(items: list[dict]) -> dict:
    t = {k: sum(1 for i in items if i["status"] == k) for k in ("agree", "differ", "missing")}
    t["objects"] = len(items)
    t["missing_by_reason"] = {}
    for i in items:
        if i["status"] == "missing":
            t["missing_by_reason"][i["reason"]] = t["missing_by_reason"].get(i["reason"], 0) + 1
    return t


def fixture_section(store: Store, scenes: list[dict], hulls: list[dict], listed: set, site: dict | None, rules: dict) -> dict:
    if not fixtures.FIXTURES.exists():
        return {"passes": [], "totals": None}
    passes = []
    dropped_cache: dict = {}

    def dropped(day_scenes):
        key = tuple(s["scene_id"] for s in day_scenes)
        if key not in dropped_cache:
            dropped_cache[key] = Dropped(store, day_scenes, site)
        return dropped_cache[key]

    for name in fixtures.s1_passes():
        m = DATE.search(name)
        day = m.group(1) if m else None
        day_scenes = [s for s in scenes if s["source"] == "s1" and s["capture_utc"][:10] == day]
        entry = {"name": name, "source": "s1", "date": day, "scenes": [s["scene_id"] for s in day_scenes]}
        if not day_scenes:
            passes.append(entry | {"covered": False, "items": [], "totals": None})
            continue
        manifest = json.loads((fixtures.FIXTURES / "s1" / name / "manifest.json").read_text(encoding="utf-8"))
        crs = str(np.load(fixtures.FIXTURES / "s1" / name / f"{manifest['objects'][0]['number']:03d}.npz")["crs"])
        pool = [h for h in hulls if h["source"] == "s1" and h["scene_id"] in entry["scenes"]]
        explain = dropped(day_scenes)
        items = []
        for obj in manifest["objects"]:
            own = [d for d in obj["detections"] if d.get("own")] or obj["detections"]
            xs = [obj["start"]["x"]] + [d["x"] for d in own]
            ys = [obj["start"]["y"]] + [d["y"] for d in own]
            lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
            lat, lon = lats[0], lons[0]
            if site and not (site["bbox"][0] <= lon <= site["bbox"][2] and site["bbox"][1] <= lat <= site["bbox"][3]):
                items.append({"key": obj["number"], "label_user": obj.get("label_user"), "fixture_verdict": obj["expected"]["verdict"], "lat": lat, "lon": lon, "status": "outside_area", "reason": "outside_hot_spot", "measures": {}, "run_verdict": None, "distance_m": None, "hull": None})
                continue
            hit = nearest(pool, lat, lon)
            e = {"key": obj["number"], "label_user": obj.get("label_user"), "fixture_verdict": obj["expected"]["verdict"], "lat": lat, "lon": lon}
            e = compare(e, hit[0] if hit else None, hit[1] if hit else None, obj["expected"], S1_KEYS, listed)
            if hit is None:
                e |= explain.explain(list(zip(lats, lons)))
            items.append(e)
        counted = [i for i in items if i["status"] != "outside_area"]
        passes.append(entry | {"covered": True, "items": items, "totals": totals(counted) | {"outside_area": len(items) - len(counted)}})

    manifest = json.loads((fixtures.FIXTURES / "s2" / "manifest.json").read_text(encoding="utf-8"))
    sets: dict[str, list] = {}
    for it in manifest["items"]:
        if DATE.search(it["set"]):
            sets.setdefault(it["set"], []).append(it)
    for name, its in sets.items():
        day = DATE.search(name).group(1)
        day_scenes = [s for s in scenes if s["source"] == "s2" and s["capture_utc"][:10] == day]
        entry = {"name": name, "source": "s2", "date": day, "scenes": [s["scene_id"] for s in day_scenes]}
        if not day_scenes:
            passes.append(entry | {"covered": False, "items": [], "totals": None})
            continue
        pool = [h for h in hulls if h["source"] == "s2" and h["scene_id"] in entry["scenes"]]
        explain = dropped(day_scenes)
        items = []
        for it in its:
            lat, lon = it["hull"]["lat"], it["hull"]["lon"]
            hit = nearest(pool, lat, lon)
            e = {"key": it["name"], "label_user": it.get("label_user"), "fixture_verdict": it["expected"]["verdict"], "lat": lat, "lon": lon}
            e = compare(e, hit[0] if hit else None, hit[1] if hit else None, it["expected"], S2_KEYS, listed)
            if hit is None:
                e |= explain.explain([(lat, lon)])
            items.append(e)
        passes.append(entry | {"covered": True, "items": items, "totals": totals(items) | {"outside_area": 0}})

    covered = [p for p in passes if p["covered"]]
    total = {k: sum(p["totals"][k] for p in covered) for k in ("objects", "agree", "differ", "missing")}
    return {"passes": passes, "totals": total}


def collect(folder: Path, n_singles: int = 24, seed: int = 0, keep: set | None = None) -> tuple[dict, dict]:
    store = Store(folder / "state.db", folder)
    try:
        rules = rules_mod.load()
        scenes = store.scenes()
        taken: set = set()
        all_hulls = []
        records = {}
        for s in scenes:
            for rec in hull_rows(store, s):
                if rec["role"] in IGNORED_ROLES:
                    continue
                h = describe(s, rec, taken)
                h["image"] = s["source"] == "s1" or window_of(s, rec) is not None
                all_hulls.append(h)
                records[h["id"]] = (s, rec)
        singles = sorted((h for h in all_hulls if h["verdict"] == "single"), key=lambda h: h["id"])
        keep = keep or set()
        picked = {h["id"] for h in singles if h["id"] in keep}
        rest = [h for h in singles if h["id"] not in picked]
        picked |= {h["id"] for h in random.Random(seed).sample(rest, max(0, min(n_singles - len(picked), len(rest))))}
        listed = [h for h in all_hulls if h["verdict"] in ("pair", "possible_pair", "unmeasured", "moving") or h["id"] in picked or h["id"] in keep]
        listed.sort(key=lambda h: (SECTIONS.index(h["verdict"]) if h["verdict"] in SECTIONS else 9, h["time"], h["id"]))
        site = site_of(store, scenes)
        fx = fixture_section(store, scenes, all_hulls, {h["id"] for h in listed}, site, rules)
        parties: dict = {}
        columns = {r["name"] for r in store.db.execute("pragma table_info(ais_matches)")}
        for r in [] if "slot" not in columns else store.db.execute("select scene_id, hull_id, slot, name, flag, imo, mmsi, dist_a_m, dist_b_m, report_age_min from ais_matches where chosen = 1 order by slot"):
            dist = r["dist_b_m"] if r["slot"] == "b" and r["dist_b_m"] is not None else r["dist_a_m"]
            parties.setdefault(f"{r['scene_id']}__{r['hull_id']}", []).append(
                {"slot": r["slot"], "name": r["name"], "flag": r["flag"], "imo": r["imo"], "mmsi": r["mmsi"], "distance_m": dist, "age_min": r["report_age_min"]})
        coverage = {(r["scene_id"], r["site"]): r["ships"] for r in store.db.execute("select scene_id, site, ships from ais_coverage")}
        floor = rules["ais_match"]["min_coverage_ships"]
        for h in listed:
            h["ais"] = parties.get(h["id"], [])
            ships = coverage.get((h["scene_id"], h["site"]))
            h["ais_status"] = "matched" if h["ais"] else ("not_checked" if ships is None else ("no_data" if ships < floor else "no_match"))
        doc = {
            "run": run_section(store, scenes) | {"singles_sample": {"n": len(picked), "of": len(singles), "seed": seed}, "site": site},
            "scenes": scene_section(scenes),
            "hulls": listed,
            "fixtures": fx,
        }
        return clean(doc), {h["id"]: records[h["id"]] for h in listed}
    finally:
        store.close()
