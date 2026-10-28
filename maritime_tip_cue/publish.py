import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Point

from maritime_tip_cue import sanctions, task_queue
from maritime_tip_cue.config import DATA_DIR, env, settings
from maritime_tip_cue.publish_merge import merge_group
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry, report
from maritime_tip_cue.watch.geo import haversine_m
from maritime_tip_cue.watch.pgutil import iso
from maritime_tip_cue.watch.state import Store, now

QA_LABEL = {"ok": "confirmed", "wrong_verdict": "corrected", "is_pair": "corrected"}


def out_root() -> Path:
    return DATA_DIR / "publish"


def ddmmyyyy(t: str) -> str:
    return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%d-%m-%Y")


def safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def site_settings(store: Store) -> dict:
    for r in store.db.execute("select args from review.run_invocations where run_key = %s order by invocation_id desc", (store.run_key,)):
        site = (r["args"] or {}).get("hot_spot_settings")
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


def run_problem(no: int, completed: set[str], allow_legacy: bool) -> str | None:
    try:
        store = Store.open(no)
    except KeyError:
        return "no such run"
    try:
        row = store.db.execute(
            "select args, status from review.run_invocations where run_key = %s order by invocation_id desc limit 1", (store.run_key,)
        ).fetchone()
    finally:
        store.close()
    if row is None:
        return "no run record"
    if row["status"] != "done":
        return f"run status is {row['status']}"
    if ((row["args"] or {}).get("hot_spot_settings") or {}).get("pipeline") == "hotspot_test":
        return "hotspot-test run is never published to the regular feed"
    job_id = (row["args"] or {}).get("job_id")
    if job_id is None:
        return None if allow_legacy else "not created by a queue job (pass --legacy to publish it deliberately)"
    if job_id not in completed:
        return f"job {job_id[:12]} has no succeeded attempt in the ledger"
    return None


def check_runs(runs: list[int], allow_legacy: bool = False) -> None:
    completed = task_queue.client().smembers(task_queue.keys()["completed"])
    problems = {no: reason for no in runs if (reason := run_problem(no, completed, allow_legacy))}
    if problems:
        detail = "; ".join(f"run {no}: {reason}" for no, reason in sorted(problems.items()))
        raise SystemExit(f"Refusing to publish, nothing written: {detail}")


def run_job_id(store: Store) -> str | None:
    row = store.db.execute(
        "select args from review.run_invocations where run_key = %s order by invocation_id desc limit 1", (store.run_key,)
    ).fetchone()
    return (row["args"] or {}).get("job_id") if row else None


def collect(no: int) -> list[dict]:
    return collect_at(registry.folder_of(no), no)


def identity_rules(snapshot: dict | None) -> dict:
    from maritime_tip_cue.equasis import resolve as equasis

    deployed = rules_mod.load()["identity"]
    return ((snapshot or {}).get("identity") or deployed) | {key: deployed[key] for key in equasis.DEPLOYED_LIMITS}


def collect_at(path: Path, no: int, job_id: str | None = None, rules: dict | None = None, only: set | None = None, verdicts: tuple = ("pair",), enrich: bool = True) -> list[dict]:
    store = Store.open(no)
    try:
        site = site_settings(store)
        job_id = job_id or run_job_id(store)
        if enrich:
            try:
                from maritime_tip_cue.ais import match

                match.refresh_identities(store)
            except Exception as error:
                print(f"identity refresh skipped: {type(error).__name__}: {error}", file=sys.stderr)
            try:
                from maritime_tip_cue.equasis import resolve as equasis

                summary = equasis.enrich(store, identity_rules(rules))
                summary["sanctioned_ships"] = sanctions.apply(store)
                print(f"equasis: {summary}", file=sys.stderr)
            except Exception as error:
                print(f"equasis enrichment skipped: {type(error).__name__}: {error}", file=sys.stderr)
        floor = rules_mod.load()["ais_match"]["min_coverage_ships"]
        coverage = {(r["scene_id"], r["site"]): r["ships"] for r in store.db.execute("select scene_id, site, ships from review.ais_coverage where run_key = %s", (store.run_key,))}
        rows = store.db.execute(
            "select f.*, h.record, s.folder, q.status as qa, q.note as qa_note from review.findings f "
            "join review.hulls h on h.run_key = f.run_key and h.scene_id = f.scene_id and h.site = f.site and h.hull_id = f.hull_id "
            "join review.scenes s on s.run_key = f.run_key and s.scene_id = f.scene_id and s.site = f.site "
            "left join review.qa_flags q on q.run_key = f.run_key and q.hull_id = f.scene_id || '__' || f.hull_id "
            "where f.run_key = %s and f.verdict = any(%s) order by f.scene_id, f.hull_id",
            (store.run_key, list(verdicts)),
        ).fetchall()
        out = []
        for r in rows:
            if only is not None and (r["scene_id"], r["site"], r["hull_id"]) not in only:
                continue
            rec = r["record"]
            parties = {
                p["slot"]: dict(p)
                for p in store.db.execute(
                    "select m.slot, m.name, coalesce(m.imo, v.imo) as imo, coalesce(m.mmsi, v.mmsi) as mmsi, m.flag, m.dist_a_m, m.dist_b_m, m.score as match_score, c.ais_heading as ais_heading_deg, m.speed_kn as ais_speed_kn, "
                    "m.report_age_min, m.ship_id, (m.is_sanctioned or coalesce(v.sanctions_lists, '') <> '') as is_sanctioned, v.sanctions_lists, v.call_sign, v.gross_tonnage, v.dwt as equasis_dwt, v.year_built, "
                    "v.vessel_type, v.status as equasis_status, v.confidence as identity_confidence, v.source as identity_source "
                    "from review.ais_matches m left join review.vessel_identities v on v.ship_id = m.ship_id "
                    "left join review.ais_candidates c on c.run_key = m.run_key and c.scene_id = m.scene_id and c.site = m.site "
                    "and c.hull_id = m.hull_id and c.slot = m.slot and c.ship_id = m.ship_id "
                    "where m.run_key = %s and m.scene_id = %s and m.site = %s and m.hull_id = %s and m.chosen",
                    (store.run_key, r["scene_id"], r["site"], r["hull_id"]),
                )
            }
            for p in parties.values():
                p["sanctions_lists"] = p.get("sanctions_lists") or sanctions.lists_for(p.get("imo")) or None
                p["is_sanctioned"] = bool(p.get("is_sanctioned") or p["sanctions_lists"])
                p["sanctions_programs"] = sanctions.programs_for(p.get("imo")) or None
            if r["verdict"] != "pair" and not any(p["is_sanctioned"] for p in parties.values()):
                continue
            ships = coverage.get((r["scene_id"], r["site"]))
            out.append(
                {
                    "run": no, "job_id": job_id, "root": path, "scene_id": r["scene_id"], "site": r["site"], "hull_id": r["hull_id"], "source": r["source"], "verdict": r["verdict"],
                    "capture_utc": iso(r["capture_utc"]), "lat": r["lat"], "lon": r["lon"], "run_verdict": r["run_verdict"],
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


MATCHED = (255, 214, 0, 235)
UNMATCHED = (255, 255, 255, 245)
MARKER_SCALE = 1.6


def draw_markers(img: Image.Image, points: list[dict], matched: list[bool]) -> Image.Image:
    k = MARKER_SCALE
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    try:
        font = ImageFont.load_default(size=round(11 * k))
    except TypeError:
        font = ImageFont.load_default()
    for i, point in enumerate(points):
        other = points[1 - i] if len(points) > 1 else point
        dx, dy = point["fx"] - other["fx"], point["fy"] - other["fy"]
        length = math.hypot(dx, dy) or 1
        x, y = point["fx"] * img.width, point["fy"] * img.height
        px = x + dx / length * 16 * k
        py = y + dy / length * 16 * k - (16 * k if length == 1 else 0)
        fill = MATCHED if matched[i] else UNMATCHED
        draw.ellipse((x - 4 * k, y - 4 * k, x + 4 * k, y + 4 * k), fill=(0, 0, 0, 153))
        draw.ellipse((x - 3 * k, y - 3 * k, x + 3 * k, y + 3 * k), fill=fill)
        label = str(point["n"])
        text = draw.textbbox((0, 0), label, font=font)
        width = max(18 * k, (text[2] - text[0]) + 8 * k)
        box = (px - width / 2, py - 9 * k, px + width / 2, py + 9 * k)
        draw.rounded_rectangle((box[0] - k, box[1] - k, box[2] + k, box[3] + k), radius=10 * k, fill=(0, 0, 0, 128))
        draw.rounded_rectangle(box, radius=9 * k, fill=fill)
        draw.text((px - (text[2] - text[0]) / 2 - text[0], py - (text[3] - text[1]) / 2 - text[1]), label, fill=(17, 17, 17, 255), font=font)
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def write_captures(f: dict, base: str, rules: dict, root: Path, single: bool = False) -> dict:
    out = {}
    folder = root / "captures" / ddmmyyyy(f["capture_utc"]) / f["site"] / f["source"]
    folder.mkdir(parents=True, exist_ok=True)
    scene = {"source": f["source"], "folder": f["folder"], "data": f["scene_data"]}
    img = report.render(f["root"], scene, f["record"], rules, False)
    if img is not None:
        points = [] if single else report.vessel_points(f["root"], scene, f["record"], rules)
        if len(points) == 2 and all(0 <= p["fx"] <= 1 and 0 <= p["fy"] <= 1 for p in points):
            img = draw_markers(img, points, [bool(f["parties"].get(slot)) for slot in ("a", "b")])
        png = folder / f"{base}.png"
        img.save(png)
        out["png"] = png
    return out


def axial_mean(angles: list) -> float | None:
    angles = [a for a in angles if a is not None]
    if not angles:
        return None
    s = sum(math.sin(math.radians(2 * a)) for a in angles)
    c = sum(math.cos(math.radians(2 * a)) for a in angles)
    if math.hypot(s, c) < 1e-9:
        return None
    return (math.degrees(math.atan2(s, c)) / 2) % 180


def compass(map_angle: float | None) -> float | None:
    return None if map_angle is None else (90 - map_angle) % 180


def axis_gap(a: float, b: float) -> float:
    d = abs(a - b) % 180
    return min(d, 180 - d)


def vessel_headings(f: dict) -> tuple[list, str | None]:
    rec = f["record"]
    if f["source"] == "s1":
        heading = compass(rec.get("rect_angle_deg"))
        return [heading, heading], "pixel_rect" if heading is not None else None
    fix, prior = (rec.get("check") or {}).get("fix_heading"), rec.get("ai2_heading")
    if fix is None or (prior is not None and axis_gap(fix, prior) <= 0.01):
        return [None, None], None
    return [fix, fix], "fitted"


def pair_heading(f: dict) -> tuple[float | None, str | None]:
    headings, source = vessel_headings(f)
    mean = axial_mean(headings)
    return (round(mean, 1), source) if mean is not None else (None, None)


def vessel_fields(p: dict, pos, slot: str = "a") -> dict:
    dist = p.get("dist_b_m") if slot == "b" and p.get("dist_b_m") is not None else p.get("dist_a_m")
    return {
        "lat": pos[0] if pos else None,
        "lon": pos[1] if pos else None,
        "ship_id": p.get("ship_id"),
        "name": p.get("name"),
        "imo": p.get("imo"),
        "mmsi": p.get("mmsi"),
        "flag": p.get("flag"),
        "call_sign": p.get("call_sign"),
        "gross_tonnage": p.get("gross_tonnage"),
        "dwt": p.get("equasis_dwt"),
        "year_built": p.get("year_built"),
        "vessel_type": p.get("vessel_type"),
        "status": p.get("equasis_status"),
        "identity_confidence": p.get("identity_confidence"),
        "identity_source": p.get("identity_source"),
        "is_sanctioned": bool(p.get("is_sanctioned")),
        "sanctions_lists": p.get("sanctions_lists") or None,
        "ais_distance_m": dist,
        "ais_report_age_min": p.get("report_age_min"),
    }


def party_cols(f: dict, n: int, slot: str, pos) -> dict:
    return {f"vessel_{n}_{key}": value for key, value in vessel_fields(f["parties"].get(slot) or {}, pos, slot).items()}


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
        findings += complete(collect(no))
    return assemble(findings, rules)


def complete(found: list[dict]) -> list[dict]:
    stores: dict[int, Store] = {}
    for f in found:
        store = stores.setdefault(f["run"], Store.open(f["run"]))
        sc = store.scene(f["scene_id"], f["site"])
        f["scene_data"] = sc["data"]
        f["record"]["hull_id"] = f["hull_id"]
        if f["source"] == "s2":
            f["record"]["_window"] = report.window_of({"data": sc["data"]}, f["record"])
    for store in stores.values():
        store.close()
    return found


def assemble(findings: list[dict], rules: dict, root: Path | None = None, provenance: dict | None = None) -> tuple[list[dict], dict]:
    kept = dedupe(findings, rules["publish"])
    root = root or out_root()
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
            heading, heading_source = pair_heading(f)
            rel ={k: p.relative_to(root / "captures").as_posix() for k, p in f["files"].items()}
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
                    "job_id": f.get("job_id"),
                    **(provenance or {}),
                    **f.get("provenance", {}),
                    "verdict": "pair",
                    "run_verdict": f["run_verdict"],
                    "qa_status": f["qa"],
                    "qa_note": f["qa_note"] or None,
                    "measures": json.dumps(measures(f)),
                    "heading_deg": heading,
                    "heading_source": heading_source,
                    **party_cols(f, 1, "a", pos[0]),
                    **party_cols(f, 2, "b", pos[1]),
                    "ais_covered": f["ais_covered"],
                    "ais_coverage_ships": f["ais_coverage_ships"],
                    "capture_png": prefix_c + rel["png"] if "png" in rel else None,
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


def upload_tables(client, bucket: str, tables: dict, rules: dict, root: Path, job_id: str | None = None, folder: str = "sts", prefix_key: str = "findings_prefix") -> dict:
    prefix_f, prefix_c = settings()["s3"][prefix_key], settings()["s3"]["captures_prefix"]
    outcomes = {}
    for (date, site, source), (path, items) in tables.items():
        key = prefix_f + path.relative_to(root / folder).as_posix()
        by_id = {f["finding_id"]: f for f in items}

        def push(kept_rows, by_id=by_id):
            for row in kept_rows:
                for p in by_id[row["finding_id"]]["files"].values():
                    ckey = prefix_c + p.relative_to(root / "captures").as_posix()
                    client.upload_file(str(p), bucket, ckey, ExtraArgs={"ContentType": "image/png"})

        outcomes[(date, site, source)] = merge_group(
            client, bucket, prefix_f, date, key, pq.read_table(path).to_pylist(), job_id, rules["publish"], push
        )
    return outcomes


def record_and_upload(tables: dict, upload: bool, rules: dict) -> None:
    import boto3

    bucket = env("S3_BUCKET_NAME")
    client = boto3.client("s3", region_name=env("AWS_REGION")) if upload else None
    root = out_root()
    prefix_f, prefix_c = settings()["s3"]["findings_prefix"], settings()["s3"]["captures_prefix"]
    outcomes = upload_tables(client, bucket, tables, rules, root) if upload else {}
    stamp = now()
    per_run: dict[int, list] = defaultdict(list)
    for (date, site, source), (path, items) in tables.items():
        key = prefix_f + path.relative_to(root / "sts").as_posix()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        kept = set(outcomes[(date, site, source)]["kept"]) if upload else {f["finding_id"] for f in items}
        items = [f for f in items if f["finding_id"] in kept]
        for f in items:
            for fmt, p in f["files"].items():
                ckey = prefix_c + p.relative_to(root / "captures").as_posix()
                per_run[f["run"]].append(("capture", f["finding_id"], fmt, str(p), f"s3://{bucket}/{ckey}", f["scene_id"], f["site"], f["hull_id"]))
        for no in sorted({f["run"] for f in items}):
            mine = [f for f in items if f["run"] == no]
            per_run[no].append(("output", str(path), f"s3://{bucket}/{key}", date, site, source, len(items), len(items), sorted({f["scene_id"] for f in mine}), sha))
    record_entries(per_run, stamp, upload)


def record_entries(per_run: dict, stamp: str, upload: bool, sync_id: int | None = None) -> None:
    for no, entries in per_run.items():
        store = Store.open(no)
        try:
            invocation = store.db.execute("select max(invocation_id) as n from review.run_invocations where run_key = %s", (store.run_key,)).fetchone()["n"]
            with store.tx():
                for e in entries:
                    if e[0] == "capture":
                        store.db.execute(
                            "insert into review.captures (run_key, finding_id, format, local_path, s3_uri, invocation_id, created_utc, uploaded_utc, scene_id, site, hull, sync_id) "
                            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (run_key, finding_id, format) do update set "
                            "local_path = excluded.local_path, s3_uri = excluded.s3_uri, invocation_id = excluded.invocation_id, "
                            "created_utc = excluded.created_utc, uploaded_utc = excluded.uploaded_utc, scene_id = excluded.scene_id, "
                            "site = excluded.site, hull = excluded.hull, sync_id = excluded.sync_id",
                            (store.run_key, e[1], e[2], e[3], e[4], invocation, stamp, stamp if upload else None, e[5], e[6], e[7], sync_id),
                        )
                    else:
                        store.db.execute(
                            "insert into review.outputs (run_key, path, s3_uri, created_utc, capture_date, site, source, n_rows, n_pairs, n_possible, scene_ids, sha256, uploaded_utc, invocation_id, sync_id) "
                            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (run_key, path) do update set "
                            "s3_uri = excluded.s3_uri, created_utc = excluded.created_utc, capture_date = excluded.capture_date, site = excluded.site, "
                            "source = excluded.source, n_rows = excluded.n_rows, n_pairs = excluded.n_pairs, n_possible = excluded.n_possible, "
                            "scene_ids = excluded.scene_ids, sha256 = excluded.sha256, uploaded_utc = excluded.uploaded_utc, invocation_id = excluded.invocation_id, sync_id = excluded.sync_id",
                            (store.run_key, e[1], e[2], stamp, e[3], e[4], e[5], e[6], e[7], 0, ",".join(e[8]), e[9], stamp if upload else None, invocation, sync_id),
                        )
                        store.db.execute("delete from review.output_scenes where run_key = %s and path = %s", (store.run_key, e[1]))
                        with store.db.cursor() as cur:
                            cur.executemany(
                                "insert into review.output_scenes (run_key, path, scene_id) values (%s,%s,%s) on conflict do nothing",
                                [(store.run_key, e[1], scene_id) for scene_id in e[8]],
                            )
        finally:
            store.close()


def publish_run(client, bucket: str, out: Path, run_no: int, job_id: str, rules: dict, provenance: dict | None = None) -> dict:
    import shutil
    import tempfile

    from maritime_tip_cue import publish_sanctioned

    singles = tuple(rules_mod.load()["sanctioned"]["verdicts"])
    found = complete(collect_at(out, run_no, job_id, rules, verdicts=("pair", *singles)))
    root = Path(tempfile.mkdtemp(prefix="publish-"))
    try:
        kept, tables = assemble([f for f in found if f["verdict"] == "pair"], rules, root, provenance)
        outcomes = upload_tables(client, bucket, tables, rules, root, job_id) if tables else {}
        result = {
            "pairs": len(kept),
            "published": sum(len(o["kept"]) for o in outcomes.values()),
            "duplicates_skipped": sum(len(o["skipped"]) for o in outcomes.values()),
            "files": sorted(o["key"] for o in outcomes.values()),
        }
        try:
            result["sanctioned"] = publish_sanctioned.publish_findings(client, bucket, [f for f in found if f["verdict"] != "pair"], rules, root, job_id, provenance)
        except Exception as error:
            result["sanctioned"] = {"error": f"{type(error).__name__}: {error}"}
            print(f"sanctioned publish failed: {result['sanctioned']['error']}", file=sys.stderr)
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    if sys.argv[1:2] == ["sync"]:
        from maritime_tip_cue import publish_sync

        return publish_sync.main(sys.argv[2:])
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor publish")
    parser.add_argument("runs", nargs="+", type=int)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    check_runs(args.runs, args.legacy)
    rules = rules_mod.load()
    kept, tables = build(args.runs, rules)
    record_and_upload(tables, args.upload, rules)
    for (date, site, source), (path, items) in sorted(tables.items(), key=lambda kv: (datetime.strptime(kv[0][0], "%d-%m-%Y"), kv[0][1], kv[0][2])):
        named = sum(1 for f in items if f["parties"])
        dup = sum(1 for f in items if f["also"])
        print(f"sts/{date}/{site}/{source}.parquet: {len(items)} pairs, {named} with an AIS name, {dup} merged with a duplicate")
    print(f"{len(kept)} findings; {'uploaded to S3' if args.upload else 'written locally to ' + str(out_root()) + ' (dry run, --upload to send)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
