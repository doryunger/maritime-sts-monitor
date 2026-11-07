import random
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.backend import server
from maritime_tip_cue import catalogue, grid_neighbour_scan, grid_neighbours, grid_request, grid_scan, grid_status, passes
from tests.test_grid_scan import NOW, RULES, _rows, finish, scene, tile_scenes

TILES = {
    "S": {"id": "S", "bbox": [0.0, 0.0, 1.0, 1.0]},
    "A": {"id": "A", "bbox": [1.0, 0.0, 2.0, 1.0]},
    "B": {"id": "B", "bbox": [0.0, 1.0, 1.0, 2.0]},
    "C": {"id": "C", "bbox": [3.0, 0.0, 4.0, 1.0]},
}
LATER = NOW + timedelta(days=1)
GRID_RULES = dict(
    RULES, scenes_queued_with_first=1, grids_in_flight=25, first_dates_per_day=25, max_scans_per_day=50,
    neighbour_new_dates=2, neighbour_max_runs=1, request_stale_minutes=30, batch_poll_minutes=1,
)


@pytest.fixture
def ring(monkeypatch, pgdb):
    pgdb.commit()
    pgdb.autocommit = True
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    monkeypatch.delenv("STS_APP_USER", raising=False)
    for module in (grid_scan, grid_neighbour_scan, grid_status):
        monkeypatch.setattr(module, "load_grid", lambda: TILES)
        monkeypatch.setattr(module, "load_rules", lambda: dict(GRID_RULES))
    monkeypatch.setattr(catalogue, "load_rules", lambda: ({"scene": {"x": 1}}, {"scene": {}}))

    def fake_find(start, end, sites, rules=None, sources=None):
        name = sites[0]["name"]
        if name == "test_grid_C":
            return []
        return [scene("s2", f"{name}_{days}", days) | {"site": name} for days in (2, 9, 16, 23)]

    monkeypatch.setattr(passes, "find", fake_find)
    return GRID_RULES


def scan_tiles(pgdb, drawn):
    rng = random.Random(7)
    grid_scan.run(pgdb, now=NOW, rng=rng, drawn=drawn)
    finish(pgdb, "test_grid_S", tile_scenes(pgdb, "S")[0][2], pairs=1)
    finish(pgdb, "test_grid_A", tile_scenes(pgdb, "A")[0][2], pairs=0)
    grid_scan.run(pgdb, now=NOW, rng=rng, drawn=drawn)
    finish(pgdb, "test_grid_A", tile_scenes(pgdb, "A")[1][2], pairs=0)
    grid_scan.run(pgdb, now=NOW, rng=rng, drawn=drawn)


def finish_all(pgdb, pairs_on=()):
    for tile in TILES:
        for _, site, scene_id, _ in tile_scenes(pgdb, tile):
            if pgdb.execute("SELECT 1 FROM review.scenes WHERE scene_id=%s", (scene_id,)).fetchone() is None:
                finish(pgdb, site, scene_id, pairs=1 if tile in pairs_on else 0)


def labels(pgdb, rules):
    return [(run["label"], sorted(grid["tile_id"] for grid in run["grids"])) for run in grid_status.scan_runs(lambda sql: _rows(pgdb, sql), rules)]


def test_neighbours_are_the_overlapping_cells_closest_first():
    found = grid_neighbours.neighbours(TILES, ["S"])
    assert [item["tile"] for item in found] == ["B", "A"]
    assert all(item["seed"] == "S" and 100 < item["km"] < 120 for item in found)


def test_plan_rescans_a_scanned_neighbour_and_runs_a_new_one_like_any_cell(pgdb, ring):
    scan_tiles(pgdb, ["S", "A"])
    planned = grid_neighbours.plan(grid_scan.db_rows(pgdb), TILES, ring)
    assert planned["seeds"] == ["S"]
    assert [(item["tile"], item["kind"], item["cost"]) for item in planned["cells"]] == [("B", "fresh", 2), ("A", "rescan", 2)]
    assert len(planned["cells"][1]["dates"]) == 2


def test_plan_keeps_the_run_within_the_cell_and_scene_limits(pgdb, ring):
    scan_tiles(pgdb, ["S", "A"])
    planned = grid_neighbours.plan(grid_scan.db_rows(pgdb), TILES, dict(ring, max_scans_per_day=3))
    assert [item["tile"] for item in planned["cells"]] == ["B"] and [item["tile"] for item in planned["later"]] == ["A"]


def test_a_known_pairs_run_scans_the_neighbours_and_is_numbered_p_1(pgdb, ring):
    scan_tiles(pgdb, ["S", "A"])
    earlier_days = {row[3].date() for row in tile_scenes(pgdb, "A")}
    assert grid_request.request(pgdb, "tester", "start", ring, "pairs")[0]
    row = pgdb.execute("SELECT mode, run_no, batch_utc FROM processing.grid_run_requests").fetchone()
    assert row[:2] == ("pairs", 1)

    def sleep(seconds):
        finish_all(pgdb)

    grid_scan.batch(pgdb, ring, sleep=sleep, rng=random.Random(3), clock=lambda: LATER, since=LATER, mode="pairs")
    rescans = tile_scenes(pgdb, "A")
    assert [row[0] for row in rescans] == [1, 2, 3, 4]
    assert {row[3].date() for row in rescans[2:]}.isdisjoint(earlier_days) and rescans[2][3].date() != rescans[3][3].date()
    assert [row[0] for row in tile_scenes(pgdb, "B")] == [1, 2]
    assert tile_scenes(pgdb, "C") == []
    assert grid_scan.batch_finished(pgdb, LATER)
    assert labels(pgdb, ring) == [("1", ["A", "S"]), ("p-1", ["A", "B"])]
    assert dict(pgdb.execute("SELECT tile_id, outcome FROM processing.grid_visits").fetchall()) == {"S": "candidate", "A": "cold", "B": "cold"}


def test_a_pair_found_by_a_known_pairs_run_makes_its_own_neighbours_candidates_for_the_next(pgdb, ring):
    scan_tiles(pgdb, ["S", "A"])
    grid_scan.batch(pgdb, ring, sleep=lambda s: finish_all(pgdb, pairs_on=("B",)), rng=random.Random(3), clock=lambda: LATER, since=LATER, mode="pairs")
    planned = grid_neighbours.plan(grid_scan.db_rows(pgdb), TILES, ring)
    assert planned["seeds"] == ["B", "S"]
    assert [item["tile"] for item in planned["skipped"]] == ["A"] and planned["skipped"][0]["reason"] == "covered"
    assert planned["cells"] == []


def test_a_neighbour_with_no_imagery_is_skipped_and_the_run_still_finishes(pgdb, ring, monkeypatch):
    scan_tiles(pgdb, ["S", "A"])
    real = passes.find

    def only_new_cells(start, end, sites, rules=None, sources=None):
        return [] if sites[0]["name"] == "test_grid_A" and (end - start).days >= 30 and end > NOW + timedelta(hours=1) else real(start, end, sites, rules, sources)

    monkeypatch.setattr(passes, "find", only_new_cells)
    grid_scan.batch(pgdb, ring, sleep=lambda s: finish_all(pgdb), rng=random.Random(3), clock=lambda: LATER, since=LATER, mode="pairs")
    assert [row[0] for row in tile_scenes(pgdb, "A")] == [1, 2]
    assert dict(pgdb.execute("SELECT tile_id, outcome FROM processing.grid_visits").fetchall())["A"] == "no_imagery"
    assert dict(labels(pgdb, ring)) == {"1": ["A", "S"], "p-1": ["A", "B"]}
    grid = {entry["tile_id"]: entry["status"] for entry in grid_status.scan_runs(lambda sql: _rows(pgdb, sql), ring)[-1]["grids"]}
    assert grid["A"] == "no_imagery"


def test_a_known_pairs_run_resumes_without_queueing_its_rescans_twice(pgdb, ring):
    scan_tiles(pgdb, ["S", "A"])
    grid_neighbour_scan.draw(pgdb, LATER, ring)
    first = grid_neighbour_scan.queue_rescans(pgdb, LATER, LATER, random.Random(3))
    assert len(first["queued"]) == 2
    assert grid_neighbour_scan.queue_rescans(pgdb, LATER, LATER, random.Random(3)) is None
    assert grid_neighbour_scan.draw(pgdb, LATER, ring) == ["B", "A"]


def test_the_page_offers_continue_in_the_mode_of_the_unfinished_run(pgdb, ring):
    scan_tiles(pgdb, ["S", "A"])
    api = TestClient(server.app)
    assert api.get("/api/hotspot-tests/grid/process?mode=pairs").json()["label"] == "p-1"
    assert api.get("/api/hotspot-tests/grid/process").json()["label"] == "2"
    assert api.post("/api/hotspot-tests/grid/process", json={"action": "start", "mode": "pairs"}).status_code == 202
    pgdb.execute("UPDATE processing.grid_run_requests SET status='failed', finished_utc=now()")
    grid_neighbour_scan.draw(pgdb, LATER, ring)
    current = api.get("/api/hotspot-tests/grid/process").json()
    assert (current["action"], current["mode"], current["label"]) == ("continue", "pairs", "p-1")
    assert api.get("/api/hotspot-tests/grid/process?mode=regular").json()["label"] == "p-1"


def test_a_known_pairs_run_is_refused_when_no_cell_is_left_and_a_bad_mode_is_rejected(pgdb, ring):
    api = TestClient(server.app)
    refused = api.post("/api/hotspot-tests/grid/process", json={"action": "start", "mode": "pairs"})
    assert refused.status_code == 409 and "No cell" in refused.json()["detail"]
    assert api.post("/api/hotspot-tests/grid/process", json={"action": "start", "mode": "other"}).status_code == 400
    assert pgdb.execute("SELECT COUNT(*) FROM processing.grid_run_requests").fetchone()[0] == 0
