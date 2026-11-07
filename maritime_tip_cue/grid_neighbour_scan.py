import random
import uuid
from datetime import UTC, datetime

from maritime_tip_cue import catalogue, grid_neighbours, grid_scan
from maritime_tip_cue.grid_status import load_grid, load_rules


def draw(db, since, rules):
    existing = catalogue.rows(db, "SELECT tile_id FROM processing.grid_draws WHERE batch_utc=%s ORDER BY position", (since,))
    if existing:
        return [row["tile_id"] for row in existing]
    planned = grid_neighbours.plan(grid_scan.db_rows(db), load_grid(), rules)
    with catalogue.tx(db):
        for position, item in enumerate(planned["cells"], 1):
            db.execute(
                "INSERT INTO processing.grid_draws (batch_utc,tile_id,round,position,kind) VALUES (%s,%s,%s,%s,%s)",
                (since, item["tile"], planned["round"], position, item["kind"]),
            )
    return [item["tile"] for item in planned["cells"]]


def waiting_rescans(db, since):
    return catalogue.rows(
        db,
        "SELECT d.tile_id,d.round FROM processing.grid_draws d WHERE d.batch_utc=%s AND d.kind='rescan' "
        "AND NOT EXISTS (SELECT 1 FROM processing.grid_scans g WHERE g.tile_id=d.tile_id AND g.round=d.round AND g.queued_utc>=d.batch_utc) "
        "AND NOT EXISTS (SELECT 1 FROM processing.grid_visits v WHERE v.tile_id=d.tile_id AND v.round=d.round AND v.decided_utc>=d.batch_utc) "
        "ORDER BY d.position",
        (since,),
    )


def queue_rescans(db, since, now=None, rng=None):
    waiting = waiting_rescans(db, since)
    if not waiting:
        return None
    now = now or datetime.now(UTC)
    rng = rng or random.SystemRandom()
    grid, rules = load_grid(), load_rules()
    raw_rules, scene_rules = catalogue.load_rules()
    scan_id = uuid.uuid4().hex
    summary = {"grid_scan": True, "neighbours": True, "queued": [], "no_imagery": [], "errors": []}
    with catalogue.controller_lock(db):
        with catalogue.tx(db):
            db.execute(
                "INSERT INTO processing.scans (scan_id,started_utc,status,summary_json) VALUES (%s,%s,'ok',%s)",
                (scan_id, now, catalogue.jsonb({"grid_scan": True, "neighbours": True, "channels": []})),
            )
        for cell in waiting:
            tile_id, round_no = cell["tile_id"], cell["round"]
            tile = grid[tile_id]
            site = grid_scan.site_for(tile, rules)
            used_scenes, used_days = grid_scan.used_by_tile(db, tile_id)
            try:
                picks = grid_scan.find_scenes(site, now, rules, scene_rules, used_scenes, used_days, rng, rules["neighbour_new_dates"])
                if not picks:
                    with catalogue.tx(db):
                        db.execute(
                            "INSERT INTO processing.grid_visits (tile_id,round,outcome,decided_utc) VALUES (%s,%s,'no_imagery',%s) "
                            "ON CONFLICT (tile_id,round) DO UPDATE SET outcome=EXCLUDED.outcome, decided_utc=EXCLUDED.decided_utc",
                            (tile_id, round_no, now),
                        )
                    summary["no_imagery"].append(tile_id)
                    continue
                last = catalogue.one(
                    db, "SELECT COALESCE(MAX(date_no),0) AS n FROM processing.grid_scans WHERE tile_id=%s AND round=%s", (tile_id, round_no)
                )["n"]
                job_ids = grid_scan.queue_scenes(db, scan_id, tile, site, [row for row, _ in picks], round_no, last + 1, now, raw_rules, False)
            except Exception as error:
                summary["errors"].append({"tile": tile_id, "error": str(error)})
                continue
            for date_no, ((row, days), job_id) in enumerate(zip(picks, job_ids), last + 1):
                summary["queued"].append(
                    {"tile": tile_id, "date_no": date_no, "source": row["source"], "scene": row["scene"], "time": row["time"], "window_days": days, "job_id": job_id}
                )
        summary["status"] = "failed" if summary["errors"] else "ok"
        with catalogue.tx(db):
            db.execute(
                "UPDATE processing.scans SET ended_utc=%s,status=%s,summary_json=%s WHERE scan_id=%s",
                (datetime.now(UTC), summary["status"], catalogue.jsonb(summary), scan_id),
            )
    return summary
