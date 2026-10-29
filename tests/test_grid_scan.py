import random
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb
from shapely.geometry import box

from maritime_tip_cue import catalogue, grid_scan, passes

RULES = {
    "first_dates_per_day": 2,
    "max_scans_per_day": 4,
    "dates_per_tile": 2,
    "grids_in_flight": 2,
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
    assert grid_scan.budget(RULES, 0, 0) == (4, 2)
    assert grid_scan.budget(RULES, 2, 2) == (2, 0)
    assert grid_scan.budget(RULES, 4, 1) == (0, 0)
    assert grid_scan.budget(RULES, 6, 0) == (0, 0)


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
        ([scan("done", 1, 20), scan("queued")], "candidate"),
        ([scan("queued"), scan("done", 3, 20)], "candidate"),
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


def tile_scenes(pgdb, tile):
    rows = pgdb.execute(
        "SELECT date_no, site, scene, scene_time FROM processing.grid_scans WHERE tile_id=%s ORDER BY date_no", (tile,)
    ).fetchall()
    return [tuple(row) for row in rows]


def test_plan_queues_both_dates_of_a_grid_together_up_to_the_in_flight_limit(pgdb, small_grid):
    first = grid_scan.run(pgdb, now=NOW, rng=random.Random(7))
    by_tile = {}
    for item in first["queued"]:
        by_tile.setdefault(item["tile"], []).append(item)
    assert len(by_tile) == 2 and set(by_tile) < set(small_grid)
    for items in by_tile.values():
        assert [item["date_no"] for item in items] == [1, 2]
        assert items[0]["scene"] != items[1]["scene"] and items[0]["time"][:10] != items[1]["time"][:10]
    assert pgdb.execute("SELECT COUNT(*) FROM processing.jobs WHERE status='pending'").fetchone()[0] == 4
    again = grid_scan.run(pgdb, now=NOW, rng=random.Random(7))
    assert again["queued"] == [] and again["in_flight"] == 2


def test_a_pair_on_the_first_scene_finishes_the_grid_and_frees_its_slot(pgdb, small_grid):
    first = grid_scan.run(pgdb, now=NOW, rng=random.Random(7))
    tiles = sorted({item["tile"] for item in first["queued"]})
    hit, miss = tiles
    site_hit = f"test_grid_{hit}"
    first_scene = tile_scenes(pgdb, hit)[0]
    finish(pgdb, site_hit, first_scene[2], pairs=1)
    later = grid_scan.run(pgdb, now=NOW + timedelta(days=1), rng=random.Random(7))
    assert {item["tile"]: item["outcome"] for item in later["decided"]} == {hit: "candidate"}
    assert {item["tile"] for item in later["queued"]} == set(small_grid) - set(tiles)
    states = {state["id"]: state["status"] for state in grid_scan.tile_states(pgdb, small_grid)}
    assert states[hit] == "scanned_pairs" and states[miss] == "waiting"


def test_two_empty_scenes_close_a_grid_cold_and_a_new_grid_follows(pgdb, small_grid):
    first = grid_scan.run(pgdb, now=NOW, rng=random.Random(7))
    tiles = sorted({item["tile"] for item in first["queued"]})
    for tile in tiles:
        for _, site, scene_id, _ in tile_scenes(pgdb, tile):
            finish(pgdb, site, scene_id, pairs=0)
    later = grid_scan.run(pgdb, now=NOW + timedelta(days=1), rng=random.Random(7))
    assert {item["tile"]: item["outcome"] for item in later["decided"]} == {tile: "cold" for tile in tiles}
    states = {state["id"]: state for state in grid_scan.tile_states(pgdb, small_grid)}
    assert all(states[tile]["status"] == "scanned_no_pairs" and states[tile]["decision"] == "cold" for tile in tiles)
    assert {item["tile"] for item in later["queued"]} == set(small_grid) - set(tiles)


def test_a_grid_with_one_available_scene_is_queued_with_one_and_closed_without_a_second(pgdb, small_grid, monkeypatch):
    monkeypatch.setattr(grid_scan, "load_rules", lambda: dict(RULES, grids_in_flight=1))
    monkeypatch.setattr(passes, "find", lambda start, end, sites, rules=None, sources=None: [scene("s2", f"{sites[0]['name']}_only", 3) | {"site": sites[0]["name"]}])
    first = grid_scan.run(pgdb, now=NOW, rng=random.Random(1))
    (item,) = first["queued"]
    finish(pgdb, f"test_grid_{item['tile']}", item["scene"], pairs=0)
    later = grid_scan.run(pgdb, now=NOW + timedelta(hours=1), rng=random.Random(1))
    assert {entry["outcome"] for entry in later["decided"]} == {"no_second_date"}


def test_a_grid_needing_two_scenes_is_not_queued_when_only_one_scene_of_budget_is_left(pgdb, small_grid, monkeypatch):
    monkeypatch.setattr(grid_scan, "load_rules", lambda: dict(RULES, max_scans_per_day=3, grids_in_flight=3))
    result = grid_scan.run(pgdb, now=NOW, rng=random.Random(5))
    assert len(result["queued"]) == 2 and len({item["tile"] for item in result["queued"]}) == 1


def test_plan_marks_tile_without_imagery_and_never_queues_it(pgdb, small_grid, monkeypatch):
    monkeypatch.setattr(passes, "find", lambda *args, **kwargs: [])
    result = grid_scan.run(pgdb, now=NOW, rng=random.Random(2))
    assert result["queued"] == [] and result["no_imagery"]
    outcomes = {row[0] for row in pgdb.execute("SELECT outcome FROM processing.grid_visits").fetchall()}
    assert outcomes == {"no_imagery"}


def test_dry_run_writes_nothing(pgdb, small_grid):
    result = grid_scan.run(pgdb, now=NOW, rng=random.Random(3), dry_run=True)
    assert len(result["queued"]) == 4
    for table in ("processing.jobs", "processing.grid_scans", "processing.grid_visits", "processing.observations"):
        assert pgdb.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_stuck_scene_is_reported_after_wait_days(pgdb, small_grid):
    grid_scan.run(pgdb, now=NOW, rng=random.Random(4))
    later = grid_scan.run(pgdb, now=NOW + timedelta(days=4), rng=random.Random(4), dry_run=True)
    assert len(later["stuck"]) == 4


def test_find_scenes_takes_the_second_scene_from_the_wider_window_when_needed(monkeypatch):
    def fake_find(start, end, sites, rules=None, sources=None):
        days = round((end - start).days)
        return [scene("s2", "near", 5)] + ([scene("s2", "far", 45)] if days == 60 else [])

    monkeypatch.setattr(passes, "find", fake_find)
    picked = grid_scan.find_scenes({"name": "x", "bbox": [0, 0, 1, 1]}, NOW, RULES, {}, set(), set(), random.Random(0), 2)
    assert [(row["scene"], days) for row, days in picked] == [("near", 30), ("far", 60)]


def test_a_grid_can_mix_an_s1_and_an_s2_scene(pgdb, small_grid, monkeypatch):
    monkeypatch.setattr(grid_scan, "load_rules", lambda: dict(RULES, grids_in_flight=1))
    monkeypatch.setattr(
        passes,
        "find",
        lambda start, end, sites, rules=None, sources=None: [
            scene("s1", "S1A_IW_GRDH_1SDV_20261027T100000_20261027T100025_000001_000001_AAAA", 3) | {"site": sites[0]["name"]},
            scene("s2", f"{sites[0]['name']}_s2", 9) | {"site": sites[0]["name"]},
        ],
    )
    result = grid_scan.run(pgdb, now=NOW, rng=random.Random(1))
    assert result["errors"] == [] and [(item["date_no"], item["source"]) for item in result["queued"]] == [(1, "s1"), (2, "s2")]
    assert pgdb.execute("SELECT COUNT(*) FROM processing.jobs").fetchone()[0] == 2
