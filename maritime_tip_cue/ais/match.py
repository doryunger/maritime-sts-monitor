import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from maritime_tip_cue import hotspots
from maritime_tip_cue.ais import archive
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch.geo import Shore, haversine_m
from maritime_tip_cue.watch.state import AIS_MATCHES, IGNORED_ROLES, Store

PAIR_VERDICTS = ("pair",)
COLUMNS = (
    "scene_id site hull_id ship_id is_sat name imo mmsi flag seen_utc report_age_min method rep_lat rep_lon "
    "proj_lat proj_lon dist_a_m dist_b_m limit_m ais_length_m ais_width_m meas_length_m length_diff_m "
    "heading_diff_deg speed_kn chosen slot reasons is_sanctioned"
).split()
CANDIDATE_COLUMNS = (
    "scene_id site hull_id slot ship_id is_sat name type type_code gt_type_code flag dwt status state "
    "seen_utc elapsed_min captured_utc tile_utc report_age_min method before_seen_utc before_age_min "
    "after_seen_utc after_age_min bracket_gap_min rep_lat rep_lon proj_lat proj_lon distance_m limit_m "
    "ais_length_m ais_width_m meas_length_m length_diff_m ais_heading hull_heading heading_diff_deg "
    "speed_kn course rot decision reasons is_sanctioned"
).split()


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def ensure_table(store: Store) -> None:
    cols = [r["name"] for r in store.db.execute("pragma table_info(ais_matches)")]
    if "ship_id" not in cols:
        if store.db.execute("select count(*) from ais_matches").fetchone()[0]:
            raise SystemExit("ais_matches has the old schema and rows")
        store.db.execute("drop table ais_matches")
        store.db.executescript(AIS_MATCHES)


def site_bbox(store: Store, site: str) -> list[float]:
    for r in store.db.execute("select args from runs"):
        settings = json.loads(r["args"] or "{}").get("hot_spot_settings")
        if settings and settings.get("name") == site:
            return settings["bbox"]
    return next(s["bbox"] for s in hotspots.load() if s["name"] == site)


def padded(bbox: list[float], radius_km: float) -> list[float]:
    w, s, e, n = bbox
    dlat = radius_km / 110.54
    dlon = radius_km / (111.32 * math.cos(math.radians((s + n) / 2)))
    return [w - dlon, s - dlat, e + dlon, n + dlat]


def fetch_reports(capture: datetime, bbox: list[float], rule: dict):
    w, s, e, n = padded(bbox, rule["search_radius_km"])
    lo = capture - timedelta(minutes=rule["search_window_min"])
    hi = capture + timedelta(minutes=rule["search_window_min"])
    fmt = "%Y-%m-%d %H:%M:%S"
    sql = (
        "select distinct ship_id, name, is_sat, type, type_code, gt_type_code, flag, lat, lon, speed_kn, course, heading, rot, "
        "seen_utc, elapsed_min, destination, length_m, width_m, dwt, status, state, captured_utc, tile_utc, coalesce(is_sanctioned, false) as is_sanctioned from vessels "
        f"where seen_utc >= TIMESTAMPTZ '{lo.strftime(fmt)}+00' and seen_utc <= TIMESTAMPTZ '{hi.strftime(fmt)}+00' "
        f"and lat between {s} and {n} and lon between {w} and {e}"
    )
    return archive.fetch(sql).to_pylist()


def dead_reckon(rep: dict, dt_s: float) -> tuple[float, float]:
    course = rep["course"] if rep["course"] is not None else rep["heading"]
    if rep["speed_kn"] is None or course is None or course >= 360:
        return rep["lat"], rep["lon"]
    d = rep["speed_kn"] * 1852 / 3600 * dt_s
    return (
        rep["lat"] + d * math.cos(math.radians(course)) / 110540,
        rep["lon"] + d * math.sin(math.radians(course)) / (111320 * math.cos(math.radians(rep["lat"]))),
    )


def project(reports: list[dict], capture: datetime, rule: dict) -> list[dict]:
    by_ship = defaultdict(list)
    for r in reports:
        by_ship[r["ship_id"]].append(r)
    out = []
    for ship_id, rs in by_ship.items():
        before = max((r for r in rs if r["seen_utc"] <= capture), key=lambda r: r["seen_utc"], default=None)
        after = min((r for r in rs if r["seen_utc"] > capture), key=lambda r: r["seen_utc"], default=None)
        near = min((r for r in (before, after) if r), key=lambda r: abs((capture - r["seen_utc"]).total_seconds()))
        age = abs((capture - near["seen_utc"]).total_seconds()) / 60
        before_age = abs((capture - before["seen_utc"]).total_seconds()) / 60 if before else None
        after_age = abs((capture - after["seen_utc"]).total_seconds()) / 60 if after else None
        stationary = near["speed_kn"] is None or near["speed_kn"] <= rule["stationary_max_speed_kn"]
        if stationary:
            lat, lon, method = near["lat"], near["lon"], "closest"
        elif before and after and (
            rule["max_report_age_min"] is None
            or (before_age <= rule["max_report_age_min"] and after_age <= rule["max_report_age_min"])
        ):
            f = (capture - before["seen_utc"]).total_seconds() / (after["seen_utc"] - before["seen_utc"]).total_seconds()
            lat = before["lat"] + f * (after["lat"] - before["lat"])
            lon = before["lon"] + f * (after["lon"] - before["lon"])
            method = "interpolated"
        else:
            lat, lon = dead_reckon(near, (capture - near["seen_utc"]).total_seconds())
            method = "dead_reckoned"
        heading = near["heading"] if near["heading"] is not None and near["heading"] < 360 else None
        if heading is None and not stationary and near["course"] is not None and near["course"] < 360:
            heading = near["course"]
        out.append(
            near
            | {
                "lat_rep": near["lat"],
                "lon_rep": near["lon"],
                "lat": lat,
                "lon": lon,
                "age": age,
                "method": method,
                "ais_heading": heading,
                "before_seen_utc": before["seen_utc"] if before else None,
                "before_age": before_age,
                "after_seen_utc": after["seen_utc"] if after else None,
                "after_age": after_age,
                "bracket_gap": (after["seen_utc"] - before["seen_utc"]).total_seconds() / 60 if before and after else None,
            }
        )
    return out


def hull_slots(row: dict) -> dict:
    rec = json.loads(row["record"])
    chk = rec.get("check")
    if chk is not None:
        slots = []
        a_lat = chk.get("hull_a_lat") or row["lat"]
        a_lon = chk.get("hull_a_lon") or row["lon"]
        length = chk.get("hull_a_length_m") or row["ai2_length_m"]
        heading = chk.get("fix_heading") if chk.get("fix_heading") is not None else row["ai2_heading"]
        slots.append({"slot": "a", "lat": a_lat, "lon": a_lon, "length": length, "heading": heading, "limit_extra_m": 0.0})
        if chk.get("hull_b_lat") is not None:
            slots.append({"slot": "b", "lat": chk["hull_b_lat"], "lon": chk["hull_b_lon"], "length": chk.get("hull_b_length_m"), "heading": heading, "limit_extra_m": 0.0})
        return {"source": "s2", "slots": slots, "centre": (a_lat, a_lon)}
    length = rec.get("length_m") or rec.get("object_length_m")
    own = [d for d in (rec.get("object") or {}).get("detections", []) if d.get("own")]
    heading = own[0]["heading"] if own and own[0].get("heading") is not None else None
    found = rec.get("vessels") or []
    if row["verdict"] in PAIR_VERDICTS and len(found) == 2:
        slots = [{"slot": "ab"[i], "lat": v["lat"], "lon": v["lon"], "length": length, "heading": heading, "limit_extra_m": 0.0} for i, v in enumerate(found)]
    else:
        n = 2 if row["verdict"] in PAIR_VERDICTS else 1
        slots = [{"slot": "ab"[i], "lat": row["lat"], "lon": row["lon"], "length": length, "heading": heading, "limit_extra_m": 0.0} for i in range(n)]
    return {"source": "s1", "slots": slots, "centre": (row["lat"], row["lon"])}


def heading_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    d = abs(a - b) % 180
    return min(d, 180 - d)


def candidates_for(hull: dict, projected: list[dict], rule: dict) -> list[dict]:
    radius_m = rule["search_radius_km"] * 1000
    slots = hull["slots"]
    out = []
    for c in projected:
        dists = [haversine_m(c["lat"], c["lon"], s["lat"], s["lon"]) for s in slots]
        if min(dists) > radius_m + slots[0]["limit_extra_m"]:
            continue
        out.append({"c": c, "dists": dists})
    for o in out:
        c = o["c"]
        o["known"] = not c["is_sat"] and bool(c["name"])
        o["per_slot"] = []
        for s, d in zip(slots, o["dists"]):
            limit = rule["party_max_distance_m"]
            reasons = []
            if d > limit:
                reasons.append("distance")
            if rule["max_report_age_min"] is not None and c["age"] > rule["max_report_age_min"]:
                reasons.append("age")
            ldiff = abs(c["length_m"] - s["length"]) if c["length_m"] and s["length"] else None
            if rule["length_tolerance_m"] is not None and ldiff is not None and ldiff > rule["length_tolerance_m"]:
                reasons.append("length")
            hdiff = heading_diff(c["ais_heading"], s["heading"])
            if rule["heading_tolerance_deg"] is not None and hdiff is not None and hdiff > rule["heading_tolerance_deg"]:
                reasons.append("heading")
            if c["is_sat"]:
                reasons.append("satellite")
            if not c["name"]:
                reasons.append("unnamed")
            o["per_slot"].append(
                {
                    "slot": s["slot"],
                    "dist": d,
                    "limit": limit,
                    "ldiff": ldiff,
                    "hdiff": hdiff,
                    "meas_length": s["length"],
                    "hull_heading": s["heading"],
                    "reasons": reasons,
                }
            )
    return out


def assign(hulls: dict[str, dict]) -> dict[tuple[str, str], int]:
    slots = [(hid, j) for hid in sorted(hulls) for j in range(len(hulls[hid]["slots"]))]
    if not slots:
        return {}
    ships = sorted({o["c"]["ship_id"] for h in hulls.values() for o in h["cands"]})
    if not ships:
        return {}
    slot_index = {key: i for i, key in enumerate(slots)}
    ship_index = {ship: i for i, ship in enumerate(ships)}
    edges = {}
    for hid, h in hulls.items():
        for o in h["cands"]:
            ship = o["c"]["ship_id"]
            for j, ps in enumerate(o["per_slot"]):
                if not ps["reasons"]:
                    i = slot_index[(hid, j)]
                    age_s = round(o["c"]["age"] * 60)
                    distance_mm = round(ps["dist"] * 1000)
                    edges[(i, ship_index[ship])] = (distance_mm, age_s)
    if not edges:
        return {}
    age_weight = max(age_s for _, age_s in edges.values()) * len(slots) + 1
    valid_costs = {
        key: distance_mm * age_weight + age_s
        for key, (distance_mm, age_s) in edges.items()
    }
    unmatched_cost = max(valid_costs.values()) * len(slots) + 1
    invalid_cost = unmatched_cost * (len(slots) + 1)
    costs = np.full((len(slots), len(ships) + len(slots)), invalid_cost, dtype=np.int64)
    costs[:, len(ships) :] = unmatched_cost
    for (i, j), cost in valid_costs.items():
        costs[i, j] = cost
    rows, cols = linear_sum_assignment(costs)
    chosen = {}
    for i, j in zip(rows, cols):
        if j < len(ships) and (i, j) in edges:
            hid, slot = slots[i]
            chosen[(hid, ships[j])] = slot
    return chosen


def load_identities(ship_ids: list[str]) -> dict:
    if not ship_ids:
        return {}
    ids = ",".join("'" + s.replace("'", "''") + "'" for s in ship_ids)
    rows = archive.fetch(f"select ship_id, name, imo, mmsi, flag from identities where ship_id in ({ids})").to_pylist()
    return {r["ship_id"]: r for r in rows}


def refresh_identities(store: Store, loader=load_identities) -> int:
    rows = store.db.execute(
        "select rowid as id, ship_id, imo, mmsi, flag from ais_matches "
        "where chosen = 1 and ship_id is not null and (imo is null or mmsi is null or flag is null)"
    ).fetchall()
    if not rows:
        return 0
    found = loader(sorted({r["ship_id"] for r in rows}))
    changed = 0
    with store.tx():
        for r in rows:
            ident = found.get(r["ship_id"])
            if not ident:
                continue
            values = tuple(r[k] or (str(ident[k]) if ident.get(k) not in (None, "") else None) for k in ("imo", "mmsi", "flag"))
            if values != (r["imo"], r["mmsi"], r["flag"]):
                store.db.execute("update ais_matches set imo = ?, mmsi = ?, flag = ? where rowid = ?", (*values, r["id"]))
                changed += 1
    return changed


def utc_text(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def select_hulls(store: Store, verdicts: tuple, all_checked: bool, stats: bool) -> list[dict]:
    q = (
        "select h.scene_id, h.site, h.hull_id, h.lat, h.lon, h.ai2_length_m, h.ai2_width_m, h.ai2_heading, h.role, h.record, "
        "f.run_verdict, f.verdict, f.source, f.capture_utc, f.qa_status from findings f join hulls h using(scene_id, site, hull_id) "
        "where f.capture_utc is not null"
    )
    rows = [dict(r) for r in store.db.execute(q)]
    rows = [r for r in rows if r["role"] not in IGNORED_ROLES]
    if stats:
        rows = [r for r in rows if all_checked or r["qa_status"] == "ok"]
    elif not all_checked:
        rows = [r for r in rows if r["verdict"] in verdicts]
    return rows


def run_match(store: Store, rows: list[dict], rules: dict, log=print) -> dict:
    rule = rules["ais_match"]
    ensure_table(store)
    by_scene = defaultdict(list)
    for r in rows:
        by_scene[(r["scene_id"], r["site"])].append(r)
    result = {}
    for (scene_id, site), hs in sorted(by_scene.items()):
        capture = parse_time(hs[0]["capture_utc"])
        bbox = site_bbox(store, site)
        reports = fetch_reports(capture, bbox, rule)
        projected = project(reports, capture, rule)
        with store.tx():
            store.db.execute(
                "insert or replace into ais_coverage values (?,?,?,?,?,?)",
                (scene_id, site, len({r["ship_id"] for r in reports}), len(reports), rule["search_window_min"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
        hulls = {}
        for r in hs:
            h = hull_slots(r)
            h["row"] = r
            h["cands"] = candidates_for(h, projected, rule)
            hulls[r["hull_id"]] = h
        chosen = assign(hulls)
        idents = load_identities(sorted({ship for (_, ship) in chosen}))
        matches = []
        candidates = []
        for hid, h in hulls.items():
            for o in h["cands"]:
                c = o["c"]
                for j, ps in enumerate(o["per_slot"]):
                    selected = chosen.get((hid, c["ship_id"])) == j
                    decision = "selected" if selected else "rejected" if ps["reasons"] else "eligible_not_selected"
                    candidates.append(
                        (
                            scene_id, site, hid, ps["slot"], c["ship_id"], int(bool(c["is_sat"])), c["name"], c.get("type"),
                            c.get("type_code"), c.get("gt_type_code"), c.get("flag"), c.get("dwt"), c.get("status"), c.get("state"),
                            utc_text(c["seen_utc"]), c.get("elapsed_min"), utc_text(c.get("captured_utc")), utc_text(c.get("tile_utc")),
                            c["age"], c["method"], utc_text(c.get("before_seen_utc")), c.get("before_age"),
                            utc_text(c.get("after_seen_utc")), c.get("after_age"), c.get("bracket_gap"), c["lat_rep"], c["lon_rep"],
                            c["lat"], c["lon"], ps["dist"], ps["limit"], c.get("length_m"), c.get("width_m"), ps["meas_length"],
                            ps["ldiff"], c.get("ais_heading"), ps["hull_heading"], ps["hdiff"], c.get("speed_kn"), c.get("course"),
                            c.get("rot"), decision, ",".join(ps["reasons"]), int(bool(c.get("is_sanctioned"))),
                        )
                    )
                    if selected:
                        ident = idents.get(c["ship_id"])
                        dists = o["dists"]
                        matches.append(
                            (
                                scene_id, site, hid, c["ship_id"], int(bool(c["is_sat"])), (ident or {}).get("name") or c["name"],
                                (ident or {}).get("imo"), (ident or {}).get("mmsi"), (ident or {}).get("flag") or c.get("flag"),
                                utc_text(c["seen_utc"]), c["age"], c["method"], c["lat_rep"], c["lon_rep"], c["lat"], c["lon"],
                                dists[0], dists[1] if len(dists) > 1 and h["source"] == "s2" else None, ps["limit"], c.get("length_m"),
                                c.get("width_m"), ps["meas_length"], ps["ldiff"], ps["hdiff"], c.get("speed_kn"), 1,
                                ps["slot"], "ok", int(bool(c.get("is_sanctioned"))),
                            )
                        )
        with store.tx():
            store.db.executemany("delete from ais_matches where scene_id=? and site=? and hull_id=?", [(scene_id, site, r["hull_id"]) for r in hs])
            store.db.executemany("delete from ais_candidates where scene_id=? and site=? and hull_id=?", [(scene_id, site, r["hull_id"]) for r in hs])
            if matches:
                store.db.executemany(f"insert into ais_matches ({','.join(COLUMNS)}) values ({','.join('?' * len(COLUMNS))})", matches)
            if candidates:
                store.db.executemany(
                    f"insert into ais_candidates ({','.join(CANDIDATE_COLUMNS)}) values ({','.join('?' * len(CANDIDATE_COLUMNS))})",
                    candidates,
                )
        result[(scene_id, site)] = {"hulls": hulls, "projected": projected, "chosen": chosen, "capture": capture, "bbox": bbox}
        log(f"{scene_id[:40]} {site}: {len(hs)} hulls, {len(projected)} ships, {sum(1 for _ in chosen)} chosen")
    return result


def print_results(store: Store, rows: list[dict]) -> None:
    print("hull | src | verdict | QA | A | B | dist_m | age_min")
    for r in sorted(rows, key=lambda r: (r["capture_utc"], r["hull_id"])):
        ms = store.db.execute(
            "select * from ais_matches where scene_id=? and site=? and hull_id=? and chosen=1 order by slot", (r["scene_id"], r["site"], r["hull_id"])
        ).fetchall()
        by = {m["slot"]: m for m in ms}
        def nm(m):
            return "no AIS" if m is None else f"{m['name']} ({m['imo'] or m['mmsi'] or '-'})"
        a, b = by.get("a"), by.get("b")
        two = len(hull_slots(r)["slots"]) > 1
        dist = "/".join(f"{m['dist_a_m'] if m['slot'] == 'a' or r['source'] == 's1' else m['dist_b_m']:.0f}" for m in ms) or "-"
        age = "/".join(f"{m['report_age_min']:.0f}" for m in ms) or "-"
        print(f"{r['hull_id'][-14:]} | {r['source']} | {r['verdict']} | {r['qa_status'] or '-'} | {nm(a)} | {nm(b) if two else 'n/a'} | {dist} | {age}")


def pct(values: list[float]) -> str:
    if not values:
        return "n=0"
    v = np.array(values)
    return f"n={len(v)} med={np.median(v):.1f} p80={np.percentile(v, 80):.1f} p95={np.percentile(v, 95):.1f}"


def nearest_named(cands: list[dict]) -> dict | None:
    known = [o for o in cands if o["known"]]
    return min(known, key=lambda o: min(o["dists"])) if known else None


def stats(store: Store, result: dict, rules: dict) -> None:
    rule = rules["ais_match"]
    rng_seed = rule["baseline_seed"]
    clearance_m = rule["baseline_clearance_km"] * 1000
    max_m = rule["max_distance_km"] * 1000
    groups = defaultdict(lambda: {"hull": [], "age": [], "len": [], "hdg": [], "none": 0, "n": 0, "rand": [], "rand_none": 0, "rand_n": 0})
    for k, (key, res) in enumerate(sorted(result.items())):
        scene_id, site = key
        shore = Shore(res["bbox"], clearance_m)
        rng = np.random.default_rng(rng_seed + k)
        hull_points = []
        named_cands = [{"c": c, "named": not c["is_sat"] and bool(c["name"])} for c in res["projected"]]
        named_cands = [n for n in named_cands if n["named"]]
        for hid, h in res["hulls"].items():
            hull_points += [(s["lat"], s["lon"]) for s in h["slots"]]
        for hid, h in res["hulls"].items():
            g = groups[(site, h["source"])]
            g["n"] += 1
            near = nearest_named(h["cands"])
            if near is None:
                g["none"] += 1
                continue
            g["hull"].append(min(near["dists"]))
            g["age"].append(near["c"]["age"])
            ps = near["per_slot"][int(np.argmin(near["dists"]))]
            if ps["ldiff"] is not None:
                g["len"].append(ps["ldiff"])
            if ps["hdiff"] is not None:
                g["hdg"].append(ps["hdiff"])
        w, s, e, n = res["bbox"]
        by_source = defaultdict(int)
        for hid, h in res["hulls"].items():
            by_source[h["source"]] += 1
        for source, count in by_source.items():
            g = groups[(site, source)]
            made = 0
            tries = 0
            while made < count and tries < 20000:
                tries += 1
                lat, lon = rng.uniform(s, n), rng.uniform(w, e)
                if shore.distance_m(lon, lat) < clearance_m:
                    continue
                if any(haversine_m(lat, lon, a, b) < clearance_m for a, b in hull_points):
                    continue
                made += 1
                g["rand_n"] += 1
                ds = [haversine_m(lat, lon, o["c"]["lat"], o["c"]["lon"]) for o in named_cands]
                ds = [d for d in ds if d <= rule["search_radius_km"] * 1000]
                if ds:
                    g["rand"].append(min(ds))
                else:
                    g["rand_none"] += 1
    for (site, source), g in sorted(groups.items()):
        within = sum(1 for d in g["hull"] if d <= max_m)
        rwithin = sum(1 for d in g["rand"] if d <= max_m)
        print(f"\n[{site} / {source}] hulls={g['n']}  (no named candidate within {rule['search_radius_km']} km: {g['none']})")
        print(f"  nearest dist m   {pct(g['hull'])}   within {rule['max_distance_km']} km: {within}/{g['n']}")
        print(f"  report age min   {pct(g['age'])}")
        print(f"  length diff m    {pct(g['len'])}")
        print(f"  heading diff deg {pct(g['hdg'])}")
        print(f"  random sea pts   n={g['rand_n']} (none within radius: {g['rand_none']}); nearest dist m {pct(g['rand'])}   within {rule['max_distance_km']} km: {rwithin}/{g['rand_n']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--verdicts", default=",".join(PAIR_VERDICTS))
    ap.add_argument("--all-checked", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    store = Store(Path(args.run) / "state.db")
    rules = rules_mod.load()
    rows = select_hulls(store, tuple(args.verdicts.split(",")), args.all_checked, args.stats)
    if not args.stats and not args.all_checked:
        ensure_table(store)
        keep = {(r["scene_id"], r["site"], r["hull_id"]) for r in rows}
        stale = set()
        with store.tx():
            for table in ("ais_matches", "ais_candidates"):
                keys = [tuple(k) for k in store.db.execute(f"select distinct scene_id, site, hull_id from {table}") if tuple(k) not in keep]
                stale.update(keys)
                store.db.executemany(f"delete from {table} where scene_id=? and site=? and hull_id=?", keys)
        print(f"cleared AIS records of {len(stale)} hulls that are no longer pairs")
    result = run_match(store, rows, rules)
    if args.stats:
        stats(store, result, rules)
    else:
        print_results(store, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
