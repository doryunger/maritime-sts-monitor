import random
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb
from shapely.geometry import box

from maritime_tip_cue import catalogue, grid_scan, passes

RULES = {
    "first_dates_per_day": 2,
    "max_scans_per_day": 3,
    "dates_per_tile": 2,
    "window_days": 30,
    "window_max_days": 60,
    "source_order": ["s1", "s2"],
    "hit_verdicts": ["pair", "possible_pair"],
    "min_hulls_for_second_date": None,
    "min_shore_distance_m": 2000,
    "result_wait_days": 3,
}
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def scene(source, scene_id, days_ago):
    when = NOW - timedelta(days=days_ago)
    return {
        "site": "x",
        "source": source,
        "scene": scene_id,
        "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cloud": None,
        "overlap_km2": 1000,
        "geometry": box(0, 0, 1, 1),
        "item": {"id": scene_id},
    }


def scan(status, pairs=None, hulls=None):
    return {"status": status, "pairs": pairs, "hulls": hulls}


def test_repo_grid_and_rules_load():
    grid = grid_scan.load_grid()
    rules = grid_scan.load_rules()
    assert len(grid) > 100
    assert all(tile["bbox"][0] < tile["bbox"][2] and tile["bbox"][1] < tile["bbox"][3] for tile in grid.values())
    assert rules["window_max_days"] >= rules["window_days"]
    assert rules["max_scans_per_day"] >= rules["first_dates_per_day"]
    site = grid_scan.site_for(next(iter(grid.values())), rules)
    assert site["name"].startswith("test_") and site["pipeline"] == "hotspot_test"


def test_budget_reserves_room_for_seconds():
    assert grid_scan.budget(RULES, 0, 0) == (3, 2)
    assert grid_scan.budget(RULES, 2, 2) == (1, 0)
    assert grid_scan.budget(RULES, 3, 1) == (0, 0)
    assert grid_scan.budget(RULES, 5, 0) == (0, 0)


@pytest.mark.parametrize(
    "scans,expected",
    [
        ([scan("queued")], None),
        ([scan("done", 1, 20)], "candidate"),
        ([scan("done", 0, 20)], "second"),
        ([scan("done", 0, 20), scan("queued")], None),
        ([scan("done", 0, 20), scan("done", 0, 30)], "cold"),
        ([scan("done", 0, 20), scan("done", 2, 30)], "candidate"),
        ([scan("done", 0, 20), scan("failed")], "failed"),
    ],
)
def test_decide(scans, expected):
    assert grid_scan.decide(scans, RULES) == expected


def test_decide_quiet_only_when_floor_is_set():
    assert grid_scan.decide([scan("done", 0, 3)], RULES) == "second"
    assert grid_scan.decide([scan("done", 0, 3)], RULES | {"min_hulls_for_second_date": 10}) == "quiet"
    assert grid_scan.decide([scan("done", 0, 30)], RULES | {"min_hulls_for_second_date": 10}) == "second"


def test_pick_scene_prefers_s1_and_skips_used_scenes_and_days():
    found = [scene("s1", "a", 3), scene("s1", "b", 5), scene("s2", "c", 4)]
    rng = random.Random(1)
    assert {grid_scan.pick_scene(found, RULES, set(), set(), rng)["scene"] for _ in range(40)} == {"a", "b"}
    assert grid_scan.pick_scene(found, RULES, {"a"}, set(), rng)["scene"] == "b"
    day_of_a = found[0]["time"][:10]
    assert grid_scan.pick_scene(found, RULES, set(), {day_of_a}, rng)["scene"] == "b"
    assert grid_scan.pick_scene(found, RULES, {"a", "b"}, set(), rng)["scene"] == "c"
    assert grid_scan.pick_scene(found, RULES, {"a", "b", "c"}, set(), rng) is None


def test_find_scene_widens_to_sixty_days_and_stops(monkeypatch):
    windows = []

    def fake_find(start, end, sites, rules=None, sources=None):
        days = round((end - start).days)
        windows.append(days)
        return [scene("s2", "late", 45)] if days == 60 else []

    monkeypatch.setattr(passes, "find", fake_find)
    row, days = grid_scan.find_scene({"name": "x", "bbox": [0, 0, 1, 1]}, NOW, RULES, {}, set(), set(), random.Random(0))
    assert (row["scene"], days, windows) == ("late", 60, [30, 60])
    monkeypatch.setattr(passes, "find", lambda *args, **kwargs: [])
    assert grid_scan.find_scene({"name": "x", "bbox": [0, 0, 1, 1]}, NOW, RULES, {}, set(), set(), random.Random(0)) == (None, None)


@pytest.fixture
def small_grid(monkeypatch, pgdb):
    pgdb.commit()
    pgdb.autocommit = True
    tiles = {name: {"id": name, "bbox": [i, 0.0, i + 1.0, 1.0]} for i, name in enumerate(("T1", "T2", "T3"))}
    monkeypatch.setattr(grid_scan, "load_grid", lambda: tiles)
    monkeypatch.setattr(grid_scan, "load_rules", lambda: dict(RULES))
    monkeypatch.setattr(catalogue, "load_rules", lambda: ({"scene": {"x": 1}}, {"scene": {}}))

    def fake_find(start, end, sites, rules=None, sources=None):
        name = sites[0]["name"]
        days = round((end - start).days)
        rows = [scene("s2", f"{name}_a", 2), scene("s2", f"{name}_b", 9)]
        if days == 60:
            rows.append(scene("s2", f"{name}_c", 40))
        return [row | {"site": name} for row in rows]

    monkeypatch.setattr(passes, "find", fake_find)
    return tiles


def finish(pgdb, site, scene_id, pairs, checked=5):
    run_key = pgdb.execute(
        "INSERT INTO review.runs (folder, origin) VALUES (%s, 'job') RETURNING run_key", (f"{site}-{scene_id}",)
    ).fetchone()[0]
    pgdb.execute(
        "INSERT INTO review.scenes (run_key, scene_id, site, source, status, counts) VALUES (%s,%s,%s,'s2','checked',%s)",
        (run_key, scene_id, site, Jsonb({"checked": checked, "verdicts": {"pair": pairs, "single": checked - pairs}})),
    )
    pgdb.commit()


def test_plan_queues_random_tiles_then_second_date_only_without_a_hit(pgdb, small_grid):
    first = grid_scan.run(pgdb, now=NOW, rng=random.Random(7))
    assert [item["date_no"] for item in first["queued"]] == [1, 1]
    queued = {item["tile"]: item for item in first["queued"]}
    assert len(queued) == 2 and set(queued) < set(small_grid)
    jobs = pgdb.execute("SELECT site, status FROM processing.jobs ORDER BY site").fetchall()
    assert [tuple(row) for row in jobs] == sorted((f"test_grid_{tile}", "pending") for tile in queued)

    tile_hit, tile_miss = sorted(queued)
    again = grid_scan.run(pgdb, now=NOW, rng=random.Random(7))
    assert again["queued"] == [] and again["resolved"] == []

    finish(pgdb, f"test_grid_{tile_hit}", queued[tile_hit]["scene"], pairs=1)
    finish(pgdb, f"test_grid_{tile_miss}", queued[tile_miss]["scene"], pairs=0)
    second = grid_scan.run(pgdb, now=NOW + timedelta(hours=1), rng=random.Random(7))
    assert {item["tile"]: item["outcome"] for item in second["decided"]} == {tile_hit: "candidate"}
    assert [(item["tile"], item["date_no"]) for item in second["queued"]] == [(tile_miss, 2)]
    assert second["queued"][0]["time"][:10] != queued[tile_miss]["time"][:10]
    assert second["queued"][0]["scene"] != queued[tile_miss]["scene"]

    done = grid_scan.run(pgdb, now=NOW + timedelta(hours=2), rng=random.Random(7))
    assert done["queued"] == []
    states = {state["id"]: state["status"] for state in grid_scan.tile_states(pgdb, small_grid)}
    assert states[tile_hit] == "scanned_pairs" and states[tile_miss] == "scanned_no_pairs"
    assert grid_scan.status(pgdb)["today"]["scans"] == 3


def test_plan_closes_tile_cold_after_second_empty_scene_and_starts_a_new_round(pgdb, small_grid):
    for hour in range(0, 72, 24):
        grid_scan.run(pgdb, now=NOW + timedelta(hours=hour), rng=random.Random(hour))
        for row in pgdb.execute("SELECT site, scene FROM processing.grid_scans WHERE status='queued'").fetchall():
            finish(pgdb, row[0], row[1], pairs=0)
    states = grid_scan.tile_states(pgdb, small_grid)
    assert {state["status"] for state in states} <= {"scanned_no_pairs", "queued", "unscanned"}
    assert any(state["decision"] == "cold" and state["status"] == "scanned_no_pairs" for state in states)
    seen = {tuple(row) for row in pgdb.execute("SELECT tile_id, scene FROM processing.grid_scans").fetchall()}
    assert len(seen) == pgdb.execute("SELECT COUNT(*) FROM processing.grid_scans").fetchone()[0]


def test_plan_marks_tile_without_imagery_and_never_queues_it(pgdb, small_grid, monkeypatch):
    monkeypatch.setattr(passes, "find", lambda *args, **kwargs: [])
    result = grid_scan.run(pgdb, now=NOW, rng=random.Random(2))
    assert result["queued"] == [] and result["no_imagery"]
    outcomes = {row[0] for row in pgdb.execute("SELECT outcome FROM processing.grid_visits").fetchall()}
    assert outcomes == {"no_imagery"}


def test_dry_run_writes_nothing(pgdb, small_grid):
    result = grid_scan.run(pgdb, now=NOW, rng=random.Random(3), dry_run=True)
    assert len(result["queued"]) == 2
    for table in ("processing.jobs", "processing.grid_scans", "processing.grid_visits", "processing.observations"):
        assert pgdb.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_stuck_scene_is_reported_after_wait_days(pgdb, small_grid):
    grid_scan.run(pgdb, now=NOW, rng=random.Random(4))
    later = grid_scan.run(pgdb, now=NOW + timedelta(days=4), rng=random.Random(4), dry_run=True)
    assert len(later["stuck"]) == 2
