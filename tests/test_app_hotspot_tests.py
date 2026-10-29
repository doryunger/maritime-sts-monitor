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
    monkeypatch.setattr(grid_status, "load_rules", lambda: {"max_scans_per_day": 50, "first_dates_per_day": 25})
    pgdb.execute(
        "INSERT INTO processing.grid_scans (tile_id,round,date_no,site,source,scene,scene_time,job_id,queued_utc,status,pairs,hulls) "
        "VALUES ('T1',1,1,'test_east_of_sohar','s2','probe',now(),'j1',now(),'done',1,4),"
        "('T2',1,1,'test_grid_T2','s1','absent',now(),'j2',now(),'queued',NULL,NULL)"
    )
    pgdb.execute("INSERT INTO processing.grid_visits (tile_id,round,outcome,decided_utc) VALUES ('T1',1,'candidate',now())")
    pgdb.commit()
    doc = client.get("/api/hotspot-tests/grid").json()
    states = {tile["id"]: tile for tile in doc["tiles"]}
    assert {key: tile["status"] for key, tile in states.items()} == {"T1": "candidate", "T2": "in_progress", "T3": "unscanned"}
    assert states["T1"]["scans"][0]["run_no"] == test.run_no and states["T2"]["scans"][0]["run_no"] is None
    assert doc["summary"]["by_status"] == {"candidate": 1, "in_progress": 1, "unscanned": 1}
    assert doc["summary"]["today"] == {"scans": 2, "max_scans": 50, "firsts": 2, "max_firsts": 25}
    assert doc["summary"]["queued_waiting_for_result"] == 1


def test_grid_land_endpoint_serves_the_coastline(both):
    client, live, test = both
    rings = client.get("/api/hotspot-tests/grid/land").json()["rings"]
    assert rings and all(len(ring) >= 3 for ring in rings)
