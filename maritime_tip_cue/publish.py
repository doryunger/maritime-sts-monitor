import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from affine import Affine
from PIL import ImageDraw, ImageFont
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point

from maritime_tip_cue.config import DATA_DIR, env, settings
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry, report, review
from maritime_tip_cue.watch.geo import haversine_m
from maritime_tip_cue.watch.state import Store, now

QA_LABEL = {"ok": "confirmed", "wrong_verdict": "corrected", "is_pair": "corrected"}
CAPTURES_SCHEMA = """
create table if not exists captures (
    finding_id text, format text, local_path text, s3_uri text, run_id integer, created_utc text, uploaded_utc text,
    primary key (finding_id, format)
);
"""


def out_root() -> Path:
    return DATA_DIR / "publish"


def ddmmyyyy(t: str) -> str:
    return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%d-%m-%Y")


def safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def ensure_tables(store: Store) -> None:
    pk = [r["name"] for r in store.db.execute("pragma table_info(captures)") if r["pk"]]
    if pk == ["finding_id"] and not store.db.execute("select count(*) from captures").fetchone()[0]:
        store.db.execute("drop table captures")
    store.db.executescript(CAPTURES_SCHEMA)


def site_settings(store: Store) -> dict:
    for r in store.db.execute("select args from runs order by run_id desc"):
        site = json.loads(r["args"] or "{}").get("hot_spot_settings")
        if site:
            return site
    return {}


def margin(lat: float, lon: float, bbox: list[float]) -> float:
    w, s, e, n = bbox
    return min(lon - w, e - lon, lat - s, n - lat)


def vessel_positions(source: str, rec: dict, lat: float, lon: float) -> list[tuple[float, float] | None]:
    if source == "s2":
        chk = rec.get("check") or {}
        pts = [(chk.get(f"hull_{w}_lat"), chk.get(f"hull_{w}_lon")) for w in ("a", "b")]
        return [p if None not in p else None for p in pts]
    found = rec.get("vessels") or []
    return [(v["lat"], v["lon"]) for v in found[:2]] + [None] * (2 - len(found[:2]))


def collect(no: int) -> list[dict]:
    path = registry.folder_of(no)
    store = Store(path / "state.db", path)
    try:
        site = site_settings(store)
        floor = rules_mod.load()["ais_match"]["min_coverage_ships"]
        coverage = {(r["scene_id"], r["site"]): r["ships"] for r in store.db.execute("select scene_id, site, ships from ais_coverage")}
        rows = store.db.execute(
            "select f.*, h.record, s.folder, q.status as qa, q.note as qa_note from findings f "
            "join hulls h using (scene_id, site, hull_id) join scenes s using (scene_id, site) "
            "left join qa_flags q on q.hull_id = f.scene_id || '__' || f.hull_id where f.verdict = 'pair'"
        ).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["record"])
            parties = {
                p["slot"]: dict(p)
                for p in store.db.execute(
                    "select slot, name, imo, mmsi, flag, dist_a_m, dist_b_m, report_age_min, ship_id, is_sanctioned from ais_matches "
                    "where scene_id=? and site=? and hull_id=? and chosen=1",
                    (r["scene_id"], r["site"], r["hull_id"]),
                )
            }
            ships = coverage.get((r["scene_id"], r["site"]))
            out.append(
                {
                    "run": no, "root": path, "scene_id": r["scene_id"], "site": r["site"], "hull_id": r["hull_id"], "source": r["source"],
                    "capture_utc": r["capture_utc"], "lat": r["lat"], "lon": r["lon"], "run_verdict": r["run_verdict"],
                    "qa": QA_LABEL.get(r["qa"], "unreviewed"), "qa_note": r["qa_note"], "record": rec, "folder": r["folder"],
                    "parties": parties, "ais_coverage_ships": ships, "ais_covered": ships is not None and ships >= floor,
                    "bbox": site.get("bbox"), "length_m": rec.get("length_m") or (rec.get("check") or {}).get("hull_a_length_m") or rec.get("ai2_length_m"),
                }
            )
        return out
    finally:
        store.close()


def capture_time(f: dict) -> datetime:
    return datetime.fromisoformat(f["capture_utc"].replace("Z", "+00:00"))


def dedupe(findings: list[dict], rule: dict) -> list[dict]:
    order = sorted(findings, key=lambda f: -margin(f["lat"], f["lon"], f["bbox"]) if f["bbox"] else 0)
    kept: list[dict] = []
    for f in order:
        twin = None
        for k in kept:
            if k["source"] != f["source"]:
                continue
            if abs((capture_time(k) - capture_time(f)).total_seconds()) > rule["dedupe_time_s"]:
                continue
            reach = rule["dedupe_length_share"] * max(k["length_m"] or 0, f["length_m"] or 0)
            if haversine_m(k["lat"], k["lon"], f["lat"], f["lon"]) <= reach:
                twin = k
                break
        if twin is None:
            f["also"] = []
            kept.append(f)
        else:
            twin["also"].append(f"{f['site']}/run {f['run']}/{f['hull_id']}")
    return kept


def crop(f: dict, rules: dict) -> tuple[dict, Affine, str, np.ndarray] | None:
    folder = f["root"] / f["folder"]
    rec = f["record"]
    if f["source"] == "s2":
        window = rec.get("_window")
        if window is None:
            return None
        z = np.load(folder / "s2_windows" / window)
        names = sorted(k for k in z.files if k.startswith("band_"))
        tr = Affine(*[float(v) for v in z["transform"][:6]])
        h, w = z[names[0]].shape
        chk = rec.get("check") or {}
        lat, lon = chk.get("hull_a_lat") or f["lat"], chk.get("hull_a_lon") or f["lon"]
        cx, cy = warp_transform("EPSG:4326", str(z["crs"]), [lon], [lat])
        half = int(rules["review"]["crop_half_m"] / abs(tr.a))
        col0, row0 = (cx[0] - tr.c) / tr.a, (cy[0] - tr.f) / tr.e
        bands = [n[5:] for n in names]
        stack = lambda r0, r1, c0, c1: np.stack([z[n][r0:r1, c0:c1] for n in names])
    else:
        obj = rec["object"]
        z = np.load(folder / "chips" / obj["file"])
        tr = Affine(*[float(v) for v in z["transform"][:6]])
        h, w = z["vh"].shape
        half = min(int(rules["review"]["crop_half_m"] / abs(tr.a)), h // 2, w // 2)
        col0, row0 = (obj["x"] - tr.c) / tr.a, (obj["y"] - tr.f) / tr.e
        bands = ["VV", "VH"]
        stack = lambda r0, r1, c0, c1: np.stack([z["vv"][r0:r1, c0:c1], z["vh"][r0:r1, c0:c1]])
    r0, c0, r1, c1 = review.crop_box(col0, row0, half, h, w)
    return {"bands": bands, "crs": str(z["crs"])}, tr * Affine.translation(c0, r0), str(z["crs"]), stack(r0, r1, c0, c1)


def write_captures(f: dict, base: str, rules: dict, root: Path) -> dict:
    out = {}
    got = crop(f, rules)
    folder = root / "captures" / ddmmyyyy(f["capture_utc"]) / f["site"] / f["source"]
    folder.mkdir(parents=True, exist_ok=True)
    if got is not None:
        meta, transform, crs, data = got
        if f["source"] == "s2" and np.all(np.mod(data, 1) == 0) and data.min() >= 0 and data.max() <= 65535:
            data = data.astype(np.uint16)
        tif = folder / f"{base}.tif"
        with rasterio.open(tif, "w", driver="GTiff", height=data.shape[1], width=data.shape[2], count=data.shape[0], dtype=data.dtype, crs=crs, transform=transform, compress="deflate") as ds:
            ds.write(data)
            ds.descriptions = tuple(meta["bands"])
            ds.update_tags(source=f["source"], scene_id=f["scene_id"], capture_utc=f["capture_utc"])
        out["tif"] = tif
    scene = {"source": f["source"], "folder": f["folder"], "data": f["scene_data"]}
    img = report.render(f["root"], scene, f["record"], rules, False)
    if img is not None:
        png = folder / f"{base}.png"
        img.save(png)
        out["png"] = png
        points = report.vessel_points(f["root"], scene, f["record"], rules)
        if len(points) == 2 and all(0 <= p["fx"] <= 1 and 0 <= p["fy"] <= 1 for p in points):
            marked = img.copy()
            draw = ImageDraw.Draw(marked)
            font = ImageFont.load_default()
            radius = max(7, min(img.size) // 32)
            for point in points:
                x, y = round(point["fx"] * (img.width - 1)), round(point["fy"] * (img.height - 1))
                bounds = (x - radius, y - radius, x + radius, y + radius)
                draw.ellipse(bounds, fill=(0, 0, 0), outline=(255, 255, 255), width=1)
                label = str(point["n"])
                text_box = draw.textbbox((0, 0), label, font=font)
                tx = x - (text_box[2] - text_box[0]) / 2
                ty = y - (text_box[3] - text_box[1]) / 2 - text_box[1]
                draw.text((tx, ty), label, fill=(255, 255, 255), font=font)
            markers_png = folder / f"{base}_markers.png"
            marked.save(markers_png)
            out["markers_png"] = markers_png
    return out


def party_cols(f: dict, n: int, slot: str, pos) -> dict:
    p = f["parties"].get(slot) or {}
    dist = p.get("dist_b_m") if slot == "b" and p.get("dist_b_m") is not None else p.get("dist_a_m")
    return {
        f"vessel_{n}_lat": pos[0] if pos else None,
        f"vessel_{n}_lon": pos[1] if pos else None,
        f"vessel_{n}_name": p.get("name"),
        f"vessel_{n}_imo": p.get("imo"),
        f"vessel_{n}_mmsi": p.get("mmsi"),
        f"vessel_{n}_flag": p.get("flag"),
        f"vessel_{n}_is_sanctioned": bool(p.get("is_sanctioned")),
        f"vessel_{n}_ais_distance_m": dist,
        f"vessel_{n}_ais_report_age_min": p.get("report_age_min"),
    }


def measures(f: dict) -> dict:
    rec = f["record"]
    if f["source"] == "s1":
        keys = ("length_m", "width_beams", "light_separation", "overlap", "fill", "blur_m")
        return {k: rec.get(k) for k in keys}
    chk = rec.get("check") or {}
    keys = ("width_ratio", "ratio_ab", "hull_a_length_m", "hull_a_width_m", "hull_b_length_m", "hull_b_width_m", "hull_b_source",
            "shape_step_beams", "shape_step_k", "shape_continuity", "shape_straight_k")
    return {k: chk.get(k) for k in keys}


def build(runs: list[int], rules: dict) -> tuple[list[dict], dict]:
    findings = []
    for no in runs:
        for f in collect(no):
            store = Store(f["root"] / "state.db", f["root"])
            try:
                sc = store.scene(f["scene_id"], f["site"])
            finally:
                store.close()
            f["scene_data"] = sc["data"]
            f["record"]["hull_id"] = f["hull_id"]
            if f["source"] == "s2":
                f["record"]["_window"] = report.window_of({"data": sc["data"]}, f["record"])
            findings.append(f)
    kept = dedupe(findings, rules["publish"])
    root = out_root()
    groups: dict[tuple, list] = defaultdict(list)
    for f in kept:
        stamp = capture_time(f).strftime("%Y%m%dT%H%M%S")
        f["finding_id"] = f"{f['source']}_{stamp}_{safe(f['hull_id'])}"
        names = [safe(p["name"]) for slot in ("a", "b") if (p := f["parties"].get(slot)) and p.get("name")]
        base = "_".join([f["finding_id"]] + names)
        f["files"] = write_captures(f, base, rules, root)
        groups[(ddmmyyyy(f["capture_utc"]), f["site"], f["source"])].append(f)
    prefix_c = settings()["s3"]["captures_prefix"]
    tables = {}
    for (date, site, source), items in sorted(groups.items()):
        rows = []
        for f in sorted(items, key=lambda f: (f["capture_utc"], f["hull_id"])):
            pos = vessel_positions(f["source"], f["record"], f["lat"], f["lon"])
            rel = {k: p.relative_to(root / "captures").as_posix() for k, p in f["files"].items()}
            rows.append(
                {
                    "finding_id": f["finding_id"],
                    "geometry": Point(f["lon"], f["lat"]).wkb,
                    "lat": f["lat"], "lon": f["lon"],
                    "capture_utc": capture_time(f),
                    "capture_date": date,
                    "hot_spot": site,
                    "also_in": ";".join(f["also"]) or None,
                    "source": source,
                    "scene_id": f["scene_id"],
                    "hull_id": f["hull_id"],
                    "run": f["run"],
                    "verdict": "pair",
                    "run_verdict": f["run_verdict"],
                    "qa_status": f["qa"],
                    "qa_note": f["qa_note"] or None,
                    "measures": json.dumps(measures(f)),
                    **party_cols(f, 1, "a", pos[0]),
                    **party_cols(f, 2, "b", pos[1]),
                    "ais_covered": f["ais_covered"],
                    "ais_coverage_ships": f["ais_coverage_ships"],
                    "capture_tif": prefix_c + rel["tif"] if "tif" in rel else None,
                    "capture_png": prefix_c + rel["png"] if "png" in rel else None,
                    "capture_markers_png": prefix_c + rel["markers_png"] if "markers_png" in rel else None,
                }
            )
        table = pa.Table.from_pylist(rows)
        geo = {"version": "1.0.0", "primary_column": "geometry", "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Point"]}}}
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), b"geo": json.dumps(geo).encode()})
        path = root / "sts" / date / site / f"{source}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path)
        tables[(date, site, source)] = (path, items)
    return kept, tables


def record_and_upload(tables: dict, upload: bool) -> None:
    import boto3

    bucket = env("S3_BUCKET_NAME")
    client = boto3.client("s3", region_name=env("AWS_REGION")) if upload else None
    root = out_root()
    prefix_f, prefix_c = settings()["s3"]["findings_prefix"], settings()["s3"]["captures_prefix"]
    stamp = now()
    per_run: dict[int, list] = defaultdict(list)
    for (date, site, source), (path, items) in tables.items():
        key = prefix_f + path.relative_to(root / "sts").as_posix()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if upload:
            client.upload_file(str(path), bucket, key)
        for f in items:
            for fmt, p in f["files"].items():
                ckey = prefix_c + p.relative_to(root / "captures").as_posix()
                if upload:
                    client.upload_file(str(p), bucket, ckey, ExtraArgs={"ContentType": "image/tiff" if fmt == "tif" else "image/png"})
                per_run[f["run"]].append(("capture", f["finding_id"], fmt, str(p), f"s3://{bucket}/{ckey}"))
        runs = sorted({f["run"] for f in items})
        for no in runs:
            mine = [f for f in items if f["run"] == no]
            per_run[no].append(("output", str(path), f"s3://{bucket}/{key}", date, site, source, len(items), len(items), ",".join(sorted({f["scene_id"] for f in mine})), sha))
    for no, entries in per_run.items():
        path = registry.folder_of(no)
        store = Store(path / "state.db", path)
        try:
            ensure_tables(store)
            run_id = store.db.execute("select max(run_id) from runs").fetchone()[0]
            with store.tx():
                for e in entries:
                    if e[0] == "capture":
                        store.db.execute("insert or replace into captures values (?,?,?,?,?,?,?)", (e[1], e[2], e[3], e[4], run_id, stamp, stamp if upload else None))
                    else:
                        store.db.execute(
                            "insert or replace into outputs values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (e[1], e[2], stamp, e[3], e[4], e[5], e[6], e[7], 0, e[8], e[9], stamp if upload else None, run_id),
                        )
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor publish")
    parser.add_argument("runs", nargs="+", type=int)
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rules = rules_mod.load()
    kept, tables = build(args.runs, rules)
    record_and_upload(tables, args.upload)
    for (date, site, source), (path, items) in sorted(tables.items(), key=lambda kv: (datetime.strptime(kv[0][0], "%d-%m-%Y"), kv[0][1], kv[0][2])):
        named = sum(1 for f in items if f["parties"])
        dup = sum(1 for f in items if f["also"])
        print(f"sts/{date}/{site}/{source}.parquet: {len(items)} pairs, {named} with an AIS name, {dup} merged with a duplicate")
    print(f"{len(kept)} findings; {'uploaded to S3' if args.upload else 'written locally to ' + str(out_root()) + ' (dry run, --upload to send)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
