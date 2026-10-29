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


def tile_states(rows, grid):
    visits = rows(
        "SELECT DISTINCT ON (tile_id) tile_id,round,outcome FROM processing.grid_visits ORDER BY tile_id,round DESC"
    )
    scans = rows(
        "SELECT g.tile_id,g.round,g.date_no,g.source,g.scene,g.scene_time,g.status,g.pairs,g.hulls,"
        "(SELECT r.run_no FROM review.scenes s JOIN review.runs r ON r.run_key=s.run_key "
        "WHERE s.site=g.site AND s.scene_id=g.scene ORDER BY s.run_key DESC LIMIT 1) AS run_no "
        "FROM processing.grid_scans g ORDER BY g.tile_id,g.round,g.date_no"
    )
    by_tile = {}
    for scan in scans:
        by_tile.setdefault(scan["tile_id"], []).append(scan)
    visited = {row["tile_id"]: row for row in visits}
    states = []
    for tile_id, tile in grid.items():
        tile_scans = by_tile.get(tile_id, [])
        latest_round = max((scan["round"] for scan in tile_scans), default=None)
        round_scans = [scan for scan in tile_scans if scan["round"] == latest_round]
        visit = visited.get(tile_id)
        if visit is not None and (not round_scans or visit["round"] >= latest_round):
            status, round_no = visit["outcome"], visit["round"]
        elif round_scans:
            status, round_no = "in_progress", latest_round
        else:
            status, round_no = "unscanned", None
        states.append(
            {
                "id": tile_id,
                "bbox": tile["bbox"],
                "status": status,
                "round": round_no,
                "scans": [
                    {key: isoformat(value) for key, value in scan.items() if key != "tile_id"} for scan in round_scans
                ],
            }
        )
    return states


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
