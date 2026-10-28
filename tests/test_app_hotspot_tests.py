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
