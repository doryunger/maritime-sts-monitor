import argparse
import math
import sys

from psycopg.rows import dict_row

from maritime_tip_cue import grid_request, grid_status, pg


def overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def centre(tile):
    x0, y0, x1, y1 = tile["bbox"]
    return (x0 + x1) / 2, (y0 + y1) / 2


def distance_km(a, b):
    (ax, ay), (bx, by) = centre(a), centre(b)
    return math.hypot((ax - bx) * 111.32 * math.cos(math.radians((ay + by) / 2)), (ay - by) * 110.57)


def neighbours(grid, seeds):
    found = []
    for tile_id, tile in grid.items():
        if tile_id in seeds:
            continue
        near = [(distance_km(tile, grid[seed]), seed) for seed in seeds if seed in grid and overlaps(tile["bbox"], grid[seed]["bbox"])]
        if near:
            km, seed = min(near)
            found.append((km, tile_id, {"tile": tile_id, "seed": seed, "km": round(km)}))
    return [item for _, _, item in sorted(found, key=lambda entry: entry[:2])]


def plan(rows, grid, rules):
    states = {state["id"]: state for state in grid_status.tile_states(rows, grid, rules)}
    seeds = sorted(tile_id for tile_id, state in states.items() if state["status"] == "scanned_pairs")
    round_no = grid_status.current_round(rows)
    visits = {
        row["tile_id"]: row["outcome"]
        for row in rows("SELECT tile_id,outcome FROM processing.grid_visits WHERE round=%s", (round_no,))
    }
    taken = {
        row["tile_id"]: row["n"]
        for row in rows(
            "SELECT tile_id, COUNT(DISTINCT batch_utc) AS n FROM processing.grid_draws WHERE kind<>'regular' GROUP BY tile_id"
        )
    }
    cells, skipped, later = [], [], []
    scenes = 0
    for item in neighbours(grid, seeds):
        scans = states[item["tile"]]["scans"]
        if taken.get(item["tile"], 0) >= rules["neighbour_max_runs"]:
            skipped.append(item | {"reason": "covered"})
            continue
        if scans:
            if any(scan["status"] not in ("done", "failed") for scan in scans):
                skipped.append(item | {"reason": "in progress"})
                continue
            if not any(scan["status"] == "done" for scan in scans):
                skipped.append(item | {"reason": "failed"})
                continue
            dates = sorted({scan["scene_time"][:10] for scan in scans if scan["status"] == "done"})
            item = item | {"kind": "rescan", "cost": rules["neighbour_new_dates"], "dates": dates}
        elif item["tile"] in visits:
            skipped.append(item | {"reason": visits[item["tile"]]})
            continue
        else:
            item = item | {"kind": "fresh", "cost": rules["dates_per_tile"], "dates": []}
        if len(cells) >= rules["first_dates_per_day"] or scenes + item["cost"] > rules["max_scans_per_day"]:
            later.append(item)
            continue
        cells.append(item)
        scenes += item["cost"]
    return {"round": round_no, "seeds": seeds, "cells": cells, "skipped": skipped, "later": later, "scenes": scenes}


def show(planned):
    print(f"round {planned['round']}, pair cells {len(planned['seeds'])}: {' '.join(planned['seeds'])}")
    print(f"run: {len(planned['cells'])} cells, up to {planned['scenes']} scenes")
    for item in planned["cells"]:
        dates = f" earlier {','.join(item['dates'])}" if item["dates"] else ""
        print(f"  {item['tile']:<6} next to {item['seed']:<6} {item['km']:>4} km  {item['kind']}{dates}")
    for item in planned["later"]:
        print(f"  {item['tile']:<6} next to {item['seed']:<6} {item['km']:>4} km  {item['kind']} (next run)")
    for item in planned["skipped"]:
        print(f"  {item['tile']:<6} next to {item['seed']:<6} {item['km']:>4} km  skipped: {item['reason']}")


def main():
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.grid_neighbours")
    parser.add_subparsers(dest="command", required=True).add_parser("plan")
    parser.parse_args()
    with pg.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        show(plan(grid_request.reader(cur), grid_status.load_grid(), grid_status.load_rules()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
