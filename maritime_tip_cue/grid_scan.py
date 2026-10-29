import argparse
import json
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

from maritime_tip_cue import catalogue, grid_status, passes
from maritime_tip_cue.grid_status import decide, load_grid, load_rules

SITE_PREFIX = "test_grid_"


def site_for(tile, rules):
    return {
        "name": SITE_PREFIX + tile["id"],
        "bbox": tile["bbox"],
        "min_shore_distance_m": rules["min_shore_distance_m"],
        "pipeline": "hotspot_test",
        "area_version": 1,
        "label": f"Grid tile {tile['id']}",
    }


def budget(rules, scans_today, firsts_today):
    room = max(rules["max_scans_per_day"] - scans_today, 0)
    return room, max(min(rules["first_dates_per_day"] - firsts_today, room), 0)


def pick_scene(found, rules, used_scenes, used_days, rng):
    for source in rules["source_order"]:
        pool = [
            row
            for row in found
            if row["source"] == source and row["scene"] not in used_scenes and row["time"][:10] not in used_days
        ]
        if pool:
            return rng.choice(pool)
    return None


def find_scenes(site, now, rules, scene_rules, used_scenes, used_days, rng, count):
    used_scenes, used_days = set(used_scenes), set(used_days)
    picked = []
    for days in dict.fromkeys((rules["window_days"], rules["window_max_days"])):
        found = passes.find(now - timedelta(days=days), now, [site], rules=scene_rules)
        while len(picked) < count:
            row = pick_scene(found, rules, used_scenes, used_days, rng)
            if row is None:
                break
            picked.append((row, days))
            used_scenes.add(row["scene"])
            used_days.add(row["time"][:10])
        if len(picked) >= count:
            break
    return picked


def find_scene(site, now, rules, scene_rules, used_scenes, used_days, rng):
    picked = find_scenes(site, now, rules, scene_rules, used_scenes, used_days, rng, 1)
    return picked[0] if picked else (None, None)


def db_rows(db):
    return lambda sql, params=(): catalogue.rows(db, sql, params)


def resolve_results(db, now, rules, dry_run, summary):
    waiting = catalogue.rows(
        db, "SELECT tile_id,round,date_no,site,scene,queued_utc,job_id FROM processing.grid_scans WHERE status='queued'"
    )
    for scan in waiting:
        result = catalogue.one(
            db,
            "SELECT status,counts FROM review.scenes WHERE site=%s AND scene_id=%s ORDER BY run_key DESC LIMIT 1",
            (scan["site"], scan["scene"]),
        )
        key = (scan["tile_id"], scan["round"], scan["date_no"])
        if result is not None and result["status"] == "checked":
            counts = result["counts"] or {}
            pairs, hulls = grid_status.count_hits(counts, rules), counts.get("checked", 0)
            summary["resolved"].append({"tile": scan["tile_id"], "date_no": scan["date_no"], "pairs": pairs, "hulls": hulls})
            if not dry_run:
                db.execute(
                    "UPDATE processing.grid_scans SET status='done',pairs=%s,hulls=%s,resolved_utc=%s "
                    "WHERE tile_id=%s AND round=%s AND date_no=%s",
                    (pairs, hulls, now, *key),
                )
            continue
        job = catalogue.one(db, "SELECT status FROM processing.jobs WHERE job_id=%s", (scan["job_id"],))
        if job is not None and job["status"] == "failed":
            summary["failed"].append({"tile": scan["tile_id"], "date_no": scan["date_no"]})
            if not dry_run:
                db.execute(
                    "UPDATE processing.grid_scans SET status='failed',resolved_utc=%s "
                    "WHERE tile_id=%s AND round=%s AND date_no=%s",
                    (now, *key),
                )
        elif now - scan["queued_utc"] > timedelta(days=rules["result_wait_days"]):
            summary["stuck"].append({"tile": scan["tile_id"], "date_no": scan["date_no"], "job_id": scan["job_id"]})


def open_tiles(db, round_no):
    scans = catalogue.rows(
        db,
        "SELECT tile_id,date_no,scene,source,scene_time,status,pairs,hulls FROM processing.grid_scans "
        "WHERE round=%s AND tile_id NOT IN (SELECT tile_id FROM processing.grid_visits WHERE round=%s) "
        "ORDER BY tile_id,date_no",
        (round_no, round_no),
    )
    grouped = {}
    for scan in scans:
        grouped.setdefault(scan["tile_id"], []).append(scan)
    return grouped


def record_visit(db, tile_id, round_no, outcome, now, dry_run):
    if not dry_run:
        db.execute(
            "INSERT INTO processing.grid_visits (tile_id,round,outcome,decided_utc) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (tile_id, round_no, outcome, now),
        )


def queue_scenes(db, scan_id, tile, site, picks, round_no, first_date_no, now, raw_rules, dry_run):
    jobs = [
        (first_date_no + offset, row, catalogue.make_manifest(site, [catalogue.serialize_scene(row)], raw_rules)["job_id"])
        for offset, row in enumerate(picks)
    ]
    if dry_run:
        return [job_id for _, _, job_id in jobs]
    with catalogue.tx(db):
        for source in dict.fromkeys(row["source"] for _, row, _ in jobs):
            catalogue.record_channel(db, scan_id, site, source, [row for _, row, _ in jobs if row["source"] == source], raw_rules, False)
        for date_no, row, job_id in jobs:
            if catalogue.one(db, "SELECT 1 FROM processing.jobs WHERE job_id=%s", (job_id,)) is None:
                raise RuntimeError(f"No job was created for {row['scene']} on tile {tile['id']}")
            db.execute(
                "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued')",
                (tile["id"], round_no, date_no, site["name"], row["source"], row["scene"], row["time"], job_id, now),
            )
    return [job_id for _, _, job_id in jobs]


def used_by_tile(db, tile_id):
    found = catalogue.rows(db, "SELECT scene,scene_time FROM processing.grid_scans WHERE tile_id=%s", (tile_id,))
    return {row["scene"] for row in found}, {row["scene_time"].astimezone(UTC).date().isoformat() for row in found}


def run(db, now=None, rng=None, dry_run=False):
    now = now or datetime.now(UTC)
    rng = rng or random.SystemRandom()
    grid, rules = load_grid(), load_rules()
    raw_rules, scene_rules = catalogue.load_rules()
    scan_id = uuid.uuid4().hex
    summary = {
        "scan_id": scan_id,
        "started_utc": now.isoformat(),
        "grid_scan": True,
        "dry_run": dry_run,
        "resolved": [],
        "failed": [],
        "stuck": [],
        "decided": [],
        "queued": [],
        "no_imagery": [],
        "errors": [],
    }
    with catalogue.controller_lock(db):
        if not dry_run:
            with catalogue.tx(db):
                db.execute(
                    "INSERT INTO processing.scans (scan_id,started_utc,status,summary_json) VALUES (%s,%s,'ok',%s)",
                    (scan_id, now, catalogue.jsonb({"grid_scan": True, "channels": []})),
                )
        resolve_results(db, now, rules, dry_run, summary)
        round_no = grid_status.current_round(db_rows(db))
        pending_second = []
        in_flight = 0
        for tile_id, scans in open_tiles(db, round_no).items():
            outcome = decide(scans, rules)
            if outcome == "second":
                pending_second.append((tile_id, scans))
            elif outcome is not None:
                record_visit(db, tile_id, round_no, outcome, now, dry_run)
                summary["decided"].append({"tile": tile_id, "outcome": outcome})
            else:
                in_flight += 1
        in_flight += len(pending_second)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = catalogue.one(
            db,
            "SELECT COUNT(*) AS scans, COUNT(*) FILTER (WHERE date_no=1) AS firsts FROM processing.grid_scans "
            "WHERE queued_utc>=%s",
            (day_start,),
        )
        room, _ = budget(rules, today["scans"], today["firsts"])
        queued = 0
        for tile_id, scans in sorted(pending_second, key=lambda item: item[1][0]["scene_time"]):
            if queued >= room:
                break
            tile = grid.get(tile_id)
            if tile is None:
                continue
            site = site_for(tile, rules)
            used_scenes, used_days = used_by_tile(db, tile_id)
            try:
                row, days = find_scene(site, now, rules, scene_rules, used_scenes, used_days, rng)
                if row is None:
                    record_visit(db, tile_id, round_no, "no_second_date", now, dry_run)
                    summary["decided"].append({"tile": tile_id, "outcome": "no_second_date"})
                    continue
                (job_id,) = queue_scenes(db, scan_id, tile, site, [row], round_no, len(scans) + 1, now, raw_rules, dry_run)
            except Exception as error:
                summary["errors"].append({"tile": tile_id, "error": str(error)})
                continue
            queued += 1
            summary["queued"].append({"tile": tile_id, "date_no": len(scans) + 1, "source": row["source"], "scene": row["scene"], "time": row["time"], "window_days": days, "job_id": job_id})
        room_left = room - queued
        _, firsts_allowed = budget(rules, today["scans"] + queued, today["firsts"])
        firsts_allowed = min(firsts_allowed, max(rules["grids_in_flight"] - in_flight, 0))
        touched = {row["tile_id"] for row in catalogue.rows(db, "SELECT tile_id FROM processing.grid_scans WHERE round=%s", (round_no,))}
        touched |= {row["tile_id"] for row in catalogue.rows(db, "SELECT tile_id FROM processing.grid_visits WHERE round=%s", (round_no,))}
        pool = [tile_id for tile_id in grid if tile_id not in touched]
        if not pool and not open_tiles(db, round_no):
            round_no += 1
            pool = list(grid)
        summary["round"] = round_no
        summary["pool"] = len(pool)
        summary["in_flight"] = in_flight
        draws, firsts = 0, 0
        for tile_id in rng.sample(pool, len(pool)):
            if firsts >= firsts_allowed or draws >= 4 * max(firsts_allowed, 1):
                break
            draws += 1
            tile = grid[tile_id]
            site = site_for(tile, rules)
            used_scenes, used_days = used_by_tile(db, tile_id)
            try:
                picks = find_scenes(site, now, rules, scene_rules, used_scenes, used_days, rng, rules["dates_per_tile"])
                if not picks:
                    record_visit(db, tile_id, round_no, "no_imagery", now, dry_run)
                    summary["no_imagery"].append(tile_id)
                    continue
                if len(picks) > room_left:
                    break
                job_ids = queue_scenes(db, scan_id, tile, site, [row for row, _ in picks], round_no, 1, now, raw_rules, dry_run)
                for date_no, ((row, days), job_id) in enumerate(zip(picks, job_ids), 1):
                    summary["queued"].append({"tile": tile_id, "date_no": date_no, "source": row["source"], "scene": row["scene"], "time": row["time"], "window_days": days, "job_id": job_id})
                room_left -= len(picks)
            except Exception as error:
                summary["errors"].append({"tile": tile_id, "error": str(error)})
                continue
            firsts += 1
    summary["ended_utc"] = datetime.now(UTC).isoformat()
    summary["status"] = "failed" if summary["errors"] else "ok"
    if not dry_run:
        with catalogue.tx(db):
            db.execute(
                "UPDATE processing.scans SET ended_utc=%s,status=%s,summary_json=%s WHERE scan_id=%s",
                (datetime.now(UTC), summary["status"], catalogue.jsonb(summary), scan_id),
            )
    return summary


def tile_states(db, grid):
    return grid_status.tile_states(db_rows(db), grid, load_rules())


def status(db):
    rows = db_rows(db)
    rules = load_rules()
    return grid_status.summary(rows, grid_status.tile_states(rows, load_grid(), rules), rules)


def main():
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.grid_scan")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    sub.add_parser("export")
    args = parser.parse_args()
    db = catalogue.connect()
    try:
        if args.command == "plan":
            try:
                result = run(db, dry_run=args.dry_run)
            except BlockingIOError as error:
                print(json.dumps({"skipped": str(error)}))
                return 0
            print(json.dumps(result, default=str, indent=1))
            return 1 if result["errors"] else 0
        if args.command == "status":
            print(json.dumps(status(db), indent=1))
        else:
            print(json.dumps(tile_states(db, load_grid()), default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
