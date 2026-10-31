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


def qa_pairs_sql(rules, scene="s"):
    hits = ",".join("'" + name.replace("'", "''") + "'" for name in rules["hit_verdicts"])
    return (
        f"(SELECT COUNT(*) FROM review.findings f WHERE f.run_key={scene}.run_key AND f.scene_id={scene}.scene_id "
        f"AND f.site={scene}.site AND f.verdict IN ({hits}))"
    )


def load_scans(rows, rules):
    scans = rows(
        "SELECT g.tile_id,g.round,g.date_no,g.source,g.scene,g.scene_time,g.queued_utc,g.status,g.pairs,g.hulls,"
        "j.status AS job_status,rs.status AS result_status,rs.counts,rs.run_no,rs.updated_utc,rs.qa_pairs "
        "FROM processing.grid_scans g LEFT JOIN processing.jobs j ON j.job_id=g.job_id LEFT JOIN LATERAL "
        f"(SELECT s.status,s.counts,s.updated_utc,r.run_no,{qa_pairs_sql(rules)} AS qa_pairs "
        "FROM review.scenes s JOIN review.runs r ON r.run_key=s.run_key "
        "WHERE s.site=g.site AND s.scene_id=g.scene ORDER BY s.run_key DESC LIMIT 1) rs ON true "
        "ORDER BY g.tile_id,g.round,g.date_no"
    )
    for scan in scans:
        if scan["result_status"] != "checked":
            continue
        scan["pairs"] = scan["qa_pairs"]
        if scan["status"] == "queued":
            scan["status"], scan["hulls"] = "done", (scan["counts"] or {}).get("checked", 0)
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
        "decision": visit["outcome"] if visit is not None and visit["outcome"] not in ("candidate", "cold") else outcome,
    }


def tile_states(rows, grid, rules):
    visits = {
        (row["tile_id"], row["round"]): row
        for row in rows("SELECT tile_id,round,outcome,decided_utc FROM processing.grid_visits ORDER BY round")
    }
    latest_visit = {}
    for (tile_id, _), visit in visits.items():
        latest_visit[tile_id] = visit
    by_tile = {}
    for scan in load_scans(rows, rules):
        by_tile.setdefault(scan["tile_id"], []).append(scan)
    drawn = {
        row["tile_id"]: row["round"]
        for row in rows(
            "SELECT tile_id,round FROM processing.grid_draws WHERE batch_utc=(SELECT MAX(batch_utc) FROM processing.grid_draws)"
        )
    }
    states = []
    for tile_id, tile in grid.items():
        tile_scans = by_tile.get(tile_id, [])
        decision, round_no = None, None
        planned = tile_id in drawn and drawn[tile_id] not in {scan["round"] for scan in tile_scans} and (tile_id, drawn[tile_id]) not in visits
        if planned:
            status, round_no = "planned", drawn[tile_id]
        elif tile_scans:
            by_round = {}
            for scan in tile_scans:
                by_round.setdefault(scan["round"], []).append(scan)
            states_by_round = {no: progress(group, visits.get((tile_id, no)), rules) for no, group in by_round.items()}
            round_no = max(by_round)
            current = states_by_round[round_no]
            decision = current["decision"]
            processed = [state for state in states_by_round.values() if state["status"] == "processed"]
            if any(state["pairs"] for state in processed):
                status = "scanned_pairs"
            elif current["status"] == "processing":
                status = "processing"
            elif processed:
                status = "scanned_no_pairs"
            else:
                status = current["status"]
        elif tile_id in latest_visit:
            visit = latest_visit[tile_id]
            decision, round_no, status = visit["outcome"], visit["round"], visit["outcome"]
        else:
            status = "unscanned"
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
                    for scan in tile_scans
                ],
            }
        )
    return states


def scan_runs(rows, rules):
    visits = {
        (row["tile_id"], row["round"]): row
        for row in rows("SELECT tile_id,round,outcome,decided_utc FROM processing.grid_visits")
    }
    draws = rows("SELECT batch_utc,tile_id,round,position FROM processing.grid_draws ORDER BY batch_utc,position")
    batches = {}
    for draw in draws:
        batches.setdefault(draw["batch_utc"], []).append(draw)
    latest_batch = max(batches, default=None)
    groups = {}
    for scan in load_scans(rows, rules):
        groups.setdefault((scan["tile_id"], scan["round"]), []).append(scan)
    runs_by_key = {}
    for (tile_id, round_no), scans in groups.items():
        first = min(scans, key=lambda scan: scan["date_no"])
        owners = [
            batch
            for batch, drawn in batches.items()
            if batch <= first["queued_utc"] and any(d["tile_id"] == tile_id and d["round"] == round_no for d in drawn)
        ]
        if owners:
            key = max(owners)
        else:
            key = first["queued_utc"].astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        state = progress(scans, visits.get((tile_id, round_no)), rules)
        reviewed = next((scan for scan in scans if scan["status"] == "done" and scan["pairs"]), None)
        runs_by_key.setdefault(key, {})[tile_id] = {
            "tile_id": tile_id,
            "status": state["status"],
            "finished": isoformat(state["finished"]),
            "pairs": state["pairs"],
            "run_no": reviewed["run_no"] if reviewed else None,
            "scene": reviewed["scene"] if reviewed else None,
        }
    if latest_batch is not None:
        entries = runs_by_key.setdefault(latest_batch, {})
        for draw in batches[latest_batch]:
            if draw["tile_id"] in entries:
                continue
            visit = visits.get((draw["tile_id"], draw["round"]))
            entries[draw["tile_id"]] = {
                "tile_id": draw["tile_id"],
                "status": visit["outcome"] if visit is not None else "pending",
                "finished": isoformat(visit["decided_utc"]) if visit is not None else None,
                "pairs": 0,
                "run_no": None,
                "scene": None,
            }
    runs = []
    for number, key in enumerate(sorted(runs_by_key), 1):
        grids = sorted(runs_by_key[key].values(), key=lambda grid: grid["tile_id"])
        runs.append(
            {
                "run": number,
                "day": key.astimezone(UTC).date().isoformat(),
                "tiles": len(grids),
                "with_pairs": sum(bool(grid["pairs"]) for grid in grids),
                "pending": sum(grid["status"] == "pending" for grid in grids),
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
