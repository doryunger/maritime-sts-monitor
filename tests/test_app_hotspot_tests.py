import pytest
from fastapi.testclient import TestClient

from app.backend import runs, server
from tests.pgstore import add_scene, hull, new_store


@pytest.fixture
def both(pgdb, tmp_path, monkeypatch):
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    runs.CACHE.clear()
    runs.SCENE_CACHE.clear()
    live = new_store(monkeypatch, tmp_path)
    add_scene(live, scene_id="live", site="off_sohar", hulls=[hull("a")])
    live.finish_run(1, "done")
    test = new_store(monkeypatch, tmp_path)
    add_scene(
        test,
        scene_id="probe",
        site="test_east_of_sohar",
        hulls=[hull("p"), hull("q", "possible_pair"), hull("d", role="duplicate"), hull("s", "single")],
    )
    test.finish_run(1, "done")
    yield TestClient(server.app), live, test
    live.close()
    test.close()


def test_test_runs_never_appear_in_the_live_listings(both):
    client, live, test = both
    assert [r["area"] for r in client.get("/api/runs").json()] == ["off_sohar"]
    assert [r["scene_id"] for r in client.get("/api/scenes").json()] == ["live"]
    assert client.get(f"/api/runs/{test.run_no}").status_code == 404
    assert client.put(f"/api/runs/{test.run_no}/flags/probe__p", json={"status": "ok"}).status_code == 404


def test_the_test_page_lists_only_test_runs_with_pair_candidates_only(both):
    client, live, test = both
    listed = client.get("/api/hotspot-tests/runs").json()
    assert [r["area"] for r in listed] == ["test_east_of_sohar"]
    assert listed[0]["run_verdicts"] == {"pair": 1, "possible_pair": 1} and listed[0]["listed"] == 2
    assert client.get(f"/api/hotspot-tests/runs/{live.run_no}").status_code == 404
    page = client.get(f"/api/hotspot-tests/runs/{test.run_no}").json()
    assert {h["id"] for h in page["hulls"]} == {"probe__p", "probe__q"}


def test_test_flags_save_only_on_pair_candidates_of_test_runs(both):
    client, live, test = both
    saved = client.put(f"/api/hotspot-tests/runs/{test.run_no}/flags/probe__p", json={"status": "ok"})
    assert saved.status_code == 200
    assert client.put(f"/api/hotspot-tests/runs/{test.run_no}/flags/probe__s", json={"status": "ok"}).status_code == 404
    assert client.put(f"/api/hotspot-tests/runs/{live.run_no}/flags/live__a", json={"status": "ok"}).status_code == 404


def test_the_test_pair_counts_ignore_live_runs_and_duplicate_hulls(both):
    _, live, test = both
    counts = runs.hotspot_test_pair_counts()
    assert list(counts) == [test.run_no]
    assert counts[test.run_no] == {"run_pair": 1, "run_possible_pair": 1, "pair": 1, "possible_pair": 1}


def test_the_test_scene_index_counts_pair_candidates_only_and_stays_out_of_the_live_index(both):
    client, live, test = both
    rows = client.get("/api/hotspot-tests/scenes").json()
    assert [(r["area"], r["scene_id"], r["candidates"]) for r in rows] == [("test_east_of_sohar", "probe", 2)]
    assert rows[0]["review"] == "not started"
    assert [r["scene_id"] for r in client.get("/api/scenes").json()] == ["live"]
    etag = client.get("/api/hotspot-tests/scenes").headers["etag"]
    assert client.get("/api/hotspot-tests/scenes", headers={"If-None-Match": etag}).status_code == 304


def test_reviewing_candidates_moves_the_scene_to_done_and_accepts_real_and_not_a_pair(both):
    client, live, test = both
    base = f"/api/hotspot-tests/runs/{test.run_no}/flags"
    assert client.put(f"{base}/probe__p", json={"status": "ok"}).status_code == 200
    real = client.put(f"{base}/probe__q", json={"status": "wrong_verdict", "correct_verdict": "pair"})
    assert real.status_code == 200 and real.json()["final_verdict"] == "pair"
    assert client.get("/api/hotspot-tests/scenes").json()[0]["review"] == "done"
    rejected = client.put(f"{base}/probe__p", json={"status": "wrong_verdict", "correct_verdict": "single"})
    assert rejected.status_code == 200 and rejected.json()["final_verdict"] == "single"


def test_grid_endpoint_reports_tile_status_and_links_finished_scans_to_their_run(both, pgdb, monkeypatch):
    client, live, test = both
    from maritime_tip_cue import grid_status

    grid = {name: {"id": name, "bbox": [i, 0.0, i + 1.0, 1.0]} for i, name in enumerate(("T1", "T2", "T3"))}
    monkeypatch.setattr(grid_status, "load_grid", lambda: grid)
    monkeypatch.setattr(grid_status, "load_rules", lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25, "hit_verdicts": ["pair", "possible_pair"]})
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status,pairs,hulls) "
        "VALUES ('T1',1,1,'test_east_of_sohar','s2','probe',now(),'j1',now(),'done',1,4),"
        "('T2',1,1,'test_grid_T2','s1','absent',now(),'j2',now(),'queued',NULL,NULL)"
    )
    pgdb.execute("INSERT INTO processing.grid_visits (tile_id,round,outcome,decided_utc) VALUES ('T1',1,'candidate',now())")
    pgdb.commit()
    doc = client.get("/api/hotspot-tests/grid").json()
    states = {tile["id"]: tile for tile in doc["tiles"]}
    assert {key: tile["status"] for key, tile in states.items()} == {"T1": "scanned_pairs", "T2": "waiting", "T3": "unscanned"}
    assert states["T1"]["scans"][0]["run_no"] == test.run_no and states["T2"]["scans"][0]["run_no"] is None
    assert doc["summary"]["by_status"] == {"scanned_pairs": 1, "waiting": 1, "unscanned": 1}
    assert doc["summary"]["today"] == {"scans": 2, "max_scans": 50, "firsts": 2, "max_firsts": 25}
    assert doc["summary"]["queued_waiting_for_result"] == 1


def test_grid_land_endpoint_serves_the_coastline(both):
    client, live, test = both
    rings = client.get("/api/hotspot-tests/grid/land").json()["rings"]
    assert rings and all(len(ring) >= 3 for ring in rings)


def test_grid_map_shows_a_finished_scene_before_the_planner_resolves_it(both, pgdb, monkeypatch):
    client, live, test = both
    from maritime_tip_cue import grid_status

    grid = {name: {"id": name, "bbox": [i, 0.0, i + 1.0, 1.0]} for i, name in enumerate(("T1", "T2"))}
    monkeypatch.setattr(grid_status, "load_grid", lambda: grid)
    monkeypatch.setattr(grid_status, "load_rules", lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25, "hit_verdicts": ["pair", "possible_pair"]})
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status) "
        "VALUES ('T1',1,1,'test_east_of_sohar','s2','probe',now(),'j1',now(),'queued'),"
        "('T2',1,1,'test_east_of_sohar','s2','probe',now(),'j2',now(),'queued')"
    )
    pgdb.execute("UPDATE processing.grid_scans SET scene='absent' WHERE tile_id='T2'")
    pgdb.commit()
    tiles = {tile["id"]: tile for tile in client.get("/api/hotspot-tests/grid").json()["tiles"]}
    assert tiles["T1"]["status"] in ("scanned_pairs", "scanned_no_pairs") and tiles["T1"]["scans"][0]["status"] == "done"
    assert tiles["T1"]["scans"][0]["hulls"] is not None and tiles["T2"]["status"] == "waiting"


def test_grid_runs_endpoint_lists_one_row_per_grid_with_its_finish_date(both, pgdb, monkeypatch):
    client, live, test = both
    from maritime_tip_cue import grid_status

    monkeypatch.setattr(
        grid_status,
        "load_rules",
        lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25, "dates_per_tile": 2, "min_hulls_for_second_date": None, "hit_verdicts": ["pair", "possible_pair"]},
    )
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status) VALUES "
        "('T1',1,1,'test_east_of_sohar','s2','probe','2026-10-18T05:00:00Z','j1','2026-10-29T10:00:00Z','queued'),"
        "('T2',1,1,'test_grid_T2','s1','absent','2026-10-20T05:00:00Z','j2','2026-10-29T10:01:00Z','queued'),"
        "('T3',1,1,'test_grid_T3','s1','absent2','2026-10-21T05:00:00Z','j3','2026-10-30T10:00:00Z','queued')"
    )
    pgdb.commit()
    doc = client.get("/api/hotspot-tests/grid/runs").json()
    assert [(run["run"], run["day"], run["tiles"]) for run in doc] == [(1, "2026-10-29", 2), (2, "2026-10-30", 1)]
    t1, t2 = doc[0]["grids"]
    assert (t1["tile_id"], t1["status"], t1["pairs"], t1["run_no"]) == ("T1", "processed", 2, test.run_no)
    assert t1["finished"] is not None and t1["scene"] == "probe"
    assert (t2["tile_id"], t2["status"], t2["pairs"], t2["finished"], t2["run_no"]) == ("T2", "waiting", 0, None, None)
    assert (doc[0]["with_pairs"], doc[0]["processed"], doc[0]["waiting"]) == (1, 1, 1)


def test_grid_with_one_empty_scene_has_no_finish_date_until_its_second_scene_is_done(both, pgdb, monkeypatch):
    client, live, test = both
    from maritime_tip_cue import grid_status

    monkeypatch.setattr(
        grid_status,
        "load_rules",
        lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25, "dates_per_tile": 2, "min_hulls_for_second_date": None, "hit_verdicts": ["pair", "possible_pair"]},
    )
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status) VALUES "
        "('T1',1,1,'test_east_of_sohar','s2','probe','2026-10-18T05:00:00Z','j1','2026-10-29T10:00:00Z','queued')"
    )
    pgdb.execute("UPDATE review.scenes SET counts = jsonb_set(counts, '{verdicts}', '{\"single\": 3}') WHERE scene_id='probe'")
    pgdb.execute("UPDATE review.hulls SET verdict='single' WHERE scene_id='probe'")
    pgdb.commit()
    grid = client.get("/api/hotspot-tests/grid/runs").json()[0]["grids"][0]
    assert (grid["status"], grid["pairs"], grid["finished"]) == ("waiting", 0, None)


def test_only_a_grid_with_a_running_job_is_processing(both, pgdb, monkeypatch):
    client, live, test = both
    from maritime_tip_cue import grid_status

    monkeypatch.setattr(
        grid_status,
        "load_rules",
        lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25, "dates_per_tile": 2, "min_hulls_for_second_date": None, "hit_verdicts": ["pair", "possible_pair"]},
    )
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status) VALUES "
        "('T1',1,1,'test_grid_T1','s1','a','2026-10-20T05:00:00Z','j1','2026-10-29T10:00:00Z','queued'),"
        "('T2',1,1,'test_grid_T2','s1','b','2026-10-20T05:00:00Z','j2','2026-10-29T10:01:00Z','queued')"
    )
    for job_id, status in (("j1", "running"), ("j2", "dispatched")):
        pgdb.execute(
            "INSERT INTO processing.jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) "
            "VALUES (%s,'x','s1',now(),'{}',%s,%s)",
            (job_id, job_id, status),
        )
    pgdb.commit()
    grids = {grid["tile_id"]: grid["status"] for grid in client.get("/api/hotspot-tests/grid/runs").json()[0]["grids"]}
    assert grids == {"T1": "processing", "T2": "waiting"}


def test_the_map_accumulates_results_across_rounds(both, pgdb, monkeypatch):
    client, live, test = both
    from maritime_tip_cue import grid_status

    grid = {name: {"id": name, "bbox": [i, 0.0, i + 1.0, 1.0]} for i, name in enumerate(("T1", "T2", "T3", "T4"))}
    monkeypatch.setattr(grid_status, "load_grid", lambda: grid)
    monkeypatch.setattr(
        grid_status,
        "load_rules",
        lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25, "dates_per_tile": 2, "min_hulls_for_second_date": None, "hit_verdicts": ["pair", "possible_pair"]},
    )
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status,pairs,hulls) VALUES "
        "('T1',1,1,'test_east_of_sohar','s2','probe','2026-10-18T05:00:00Z','a1','2026-10-20T10:00:00Z','done',1,4),"
        "('T1',2,1,'test_grid_T1','s1','x1','2026-10-21T05:00:00Z','a2','2026-10-29T10:00:00Z','queued',NULL,NULL),"
        "('T2',1,1,'test_grid_T2','s1','y1','2026-10-18T05:00:00Z','b1','2026-10-20T10:00:00Z','done',0,5),"
        "('T2',1,2,'test_grid_T2','s1','y2','2026-10-19T05:00:00Z','b2','2026-10-20T10:00:00Z','done',0,5),"
        "('T2',2,1,'test_grid_T2','s1','y3','2026-10-21T05:00:00Z','b3','2026-10-29T10:00:00Z','queued',NULL,NULL),"
        "('T3',1,1,'test_grid_T3','s1','z1','2026-10-18T05:00:00Z','c1','2026-10-20T10:00:00Z','done',0,5),"
        "('T3',1,2,'test_grid_T3','s1','z2','2026-10-19T05:00:00Z','c2','2026-10-20T10:00:00Z','done',0,5),"
        "('T3',2,1,'test_grid_T3','s1','z3','2026-10-21T05:00:00Z','c3','2026-10-29T10:00:00Z','queued',NULL,NULL)"
    )
    pgdb.execute(
        "INSERT INTO processing.jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) "
        "VALUES ('c3','x','s1',now(),'{}','c3','running')"
    )
    pgdb.commit()
    tiles = {tile["id"]: tile for tile in client.get("/api/hotspot-tests/grid").json()["tiles"]}
    assert tiles["T1"]["status"] == "scanned_pairs" and len(tiles["T1"]["scans"]) == 2
    assert tiles["T2"]["status"] == "scanned_no_pairs"
    assert tiles["T3"]["status"] == "processing"
    assert tiles["T4"]["status"] == "unscanned"


def test_worker_status_is_empty_until_the_idle_check_has_recorded_one(both, pgdb):
    client, live, test = both
    assert client.get("/api/hotspot-tests/worker").json() is None
    from maritime_tip_cue import worker_status

    worker_status.record(pgdb, "running", 60, 0, (5, 5))
    pgdb.commit()
    body = client.get("/api/hotspot-tests/worker").json()
    assert (body["state"], body["activity"], body["cpu"], body["gpu"]) == ("running", "preparing imagery", 60, 0)
