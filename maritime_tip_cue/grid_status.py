import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from maritime_tip_cue.config import ROOT, settings


def resolve_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_grid():
    document = json.loads(resolve_path(settings()["grid_scan"]["grid_path"]).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or not isinstance(document.get("tiles"), list):
        raise ValueError("Unsupported grid file")
    tiles = {}
    for tile in document["tiles"]:
        bbox = tile.get("bbox")
        if not isinstance(tile.get("id"), str) or tile["id"] in tiles:
            raise ValueError("Grid tile ids must be unique strings")
        if not isinstance(bbox, list) or len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError(f"Invalid bounding box for tile {tile['id']}")
        tiles[tile["id"]] = tile
    return tiles


def load_rules():
    raw = json.loads(resolve_path(settings()["grid_scan"]["rules_path"]).read_text(encoding="utf-8"))
    return {key: entry["value"] for key, entry in raw.items() if isinstance(entry, dict) and "value" in entry}


def load_land():
    document = json.loads(resolve_path(settings()["grid_scan"]["land_path"]).read_text(encoding="utf-8"))
    return document["rings"]


def current_round(rows):
    return rows(
        "SELECT COALESCE(MAX(round), 1) AS n FROM "
        "(SELECT round FROM processing.grid_visits UNION ALL SELECT round FROM processing.grid_scans) t"
    )[0]["n"]


def isoformat(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def count_hits(counts, rules):
    verdicts = (counts or {}).get("verdicts") or {}
    return sum(verdicts.get(name, 0) for name in rules["hit_verdicts"])


def decide(scans, rules):
    if any(scan["status"] == "done" and scan["pairs"] for scan in scans):
        return "candidate"
    if any(scan["status"] == "failed" for scan in scans):
        return "failed"
    if any(scan["status"] != "done" for scan in scans):
        return None
    if len(scans) >= rules["dates_per_tile"]:
        return "cold"
    floor = rules["min_hulls_for_second_date"]
    if floor is not None and (scans[-1]["hulls"] or 0) < floor:
        return "quiet"
    return "second"


def load_scans(rows, rules):
    scans = rows(
        "SELECT g.tile_id,g.round,g.date_no,g.source,g.scene,g.scene_time,g.queued_utc,g.status,g.pairs,g.hulls,"
        "j.status AS job_status,rs.status AS result_status,rs.counts,rs.run_no,rs.updated_utc "
        "FROM processing.grid_scans g LEFT JOIN processing.jobs j ON j.job_id=g.job_id LEFT JOIN LATERAL "
        "(SELECT s.status,s.counts,s.updated_utc,r.run_no FROM review.scenes s JOIN review.runs r ON r.run_key=s.run_key "
        "WHERE s.site=g.site AND s.scene_id=g.scene ORDER BY s.run_key DESC LIMIT 1) rs ON true "
        "ORDER BY g.tile_id,g.round,g.date_no"
    )
    for scan in scans:
        if scan["status"] == "queued" and scan["result_status"] == "checked":
            counts = scan["counts"] or {}
            scan["status"], scan["pairs"], scan["hulls"] = "done", count_hits(counts, rules), counts.get("checked", 0)
    return scans


MAP_STATUS = {"waiting": "waiting", "processing": "processing", "failed": "failed"}


def progress(scans, visit, rules):
    scans = sorted(scans, key=lambda scan: scan["date_no"])
    done = [scan for scan in scans if scan["status"] == "done"]
    outcome = decide(scans, rules)
    closed_early = visit is not None and visit["outcome"] == "no_second_date"
    finished = None
    if outcome == "failed":
        status = "failed"
    elif outcome in ("candidate", "cold", "quiet") or closed_early:
        status = "processed"
        stamps = [scan["updated_utc"] for scan in done if scan["updated_utc"]]
        finished = visit["decided_utc"] if closed_early else max(stamps, default=None)
    elif any(scan["job_status"] == "running" for scan in scans if scan["status"] != "done"):
        status = "processing"
    else:
        status = "waiting"
    return {
        "status": status,
        "pairs": sum(scan["pairs"] or 0 for scan in done),
        "finished": finished,
        "decision": visit["outcome"] if visit is not None else outcome,
    }


def tile_states(rows, grid, rules):
    visits = rows(
        "SELECT DISTINCT ON (tile_id) tile_id,round,outcome,decided_utc FROM processing.grid_visits ORDER BY tile_id,round DESC"
    )
    by_tile = {}
    for scan in load_scans(rows, rules):
        by_tile.setdefault(scan["tile_id"], []).append(scan)
    visited = {row["tile_id"]: row for row in visits}
    states = []
    for tile_id, tile in grid.items():
        tile_scans = by_tile.get(tile_id, [])
        latest_round = max((scan["round"] for scan in tile_scans), default=None)
        round_scans = [scan for scan in tile_scans if scan["round"] == latest_round]
        visit = visited.get(tile_id)
        decision = None
        if visit is not None and (not round_scans or visit["round"] > latest_round):
            decision, round_no, status = visit["outcome"], visit["round"], visit["outcome"]
        elif round_scans:
            round_no = latest_round
            state = progress(round_scans, visit if visit is not None and visit["round"] == latest_round else None, rules)
            decision = state["decision"]
            if state["status"] == "processed":
                status = "scanned_pairs" if state["pairs"] else "scanned_no_pairs"
            else:
                status = MAP_STATUS[state["status"]]
        else:
            status, round_no = "unscanned", None
        states.append(
            {
                "id": tile_id,
                "bbox": tile["bbox"],
                "status": status,
                "decision": decision,
                "round": round_no,
                "scans": [
                    {
                        key: isoformat(scan[key])
                        for key in ("round", "date_no", "source", "scene", "scene_time", "status", "pairs", "hulls", "run_no")
                    }
                    for scan in round_scans
                ],
            }
        )
    return states


def scan_runs(rows, rules):
    visits = {
        (row["tile_id"], row["round"]): row
        for row in rows("SELECT tile_id,round,outcome,decided_utc FROM processing.grid_visits")
    }
    groups = {}
    for scan in load_scans(rows, rules):
        groups.setdefault((scan["tile_id"], scan["round"]), []).append(scan)
    days = {}
    for (tile_id, round_no), scans in groups.items():
        first = min(scans, key=lambda scan: scan["date_no"])
        state = progress(scans, visits.get((tile_id, round_no)), rules)
        reviewed = next((scan for scan in scans if scan["status"] == "done" and scan["pairs"]), None)
        days.setdefault(first["queued_utc"].astimezone(UTC).date().isoformat(), []).append(
            {
                "tile_id": tile_id,
                "status": state["status"],
                "finished": isoformat(state["finished"]),
                "pairs": state["pairs"],
                "run_no": reviewed["run_no"] if reviewed else None,
                "scene": reviewed["scene"] if reviewed else None,
            }
        )
    runs = []
    for number, day in enumerate(sorted(days), 1):
        grids = sorted(days[day], key=lambda grid: grid["tile_id"])
        runs.append(
            {
                "run": number,
                "day": day,
                "tiles": len(grids),
                "with_pairs": sum(bool(grid["pairs"]) for grid in grids),
                "waiting": sum(grid["status"] == "waiting" for grid in grids),
                "processing": sum(grid["status"] == "processing" for grid in grids),
                "processed": sum(grid["status"] == "processed" for grid in grids),
                "grids": grids,
            }
        )
    return runs


def summary(rows, states, rules, now=None):
    now = now or datetime.now(UTC)
    today = rows(
        "SELECT COUNT(*) AS scans, COUNT(*) FILTER (WHERE date_no=1) AS firsts FROM processing.grid_scans "
        "WHERE queued_utc>=%s",
        (now.replace(hour=0, minute=0, second=0, microsecond=0),),
    )[0]
    waiting = rows("SELECT COUNT(*) AS n FROM processing.grid_scans WHERE status='queued'")[0]["n"]
    return {
        "round": current_round(rows),
        "tiles": len(states),
        "by_status": dict(Counter(state["status"] for state in states)),
        "today": {
            "scans": today["scans"],
            "max_scans": rules["max_scans_per_day"],
            "firsts": today["firsts"],
            "max_firsts": rules["first_dates_per_day"],
        },
        "queued_waiting_for_result": waiting,
    }
