import pytest
from fastapi.testclient import TestClient

from app.backend import runs, server
from tests.pgstore import add_scene, hull, new_store


@pytest.fixture
def api(pgdb, tmp_path, monkeypatch):
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    runs.CACHE.clear()
    runs.SCENE_CACHE.clear()
    store = new_store(monkeypatch, tmp_path)
    add_scene(store, hulls=[hull("a"), hull("b", "single")])
    store.finish_run(1, "done")
    yield TestClient(server.app), store
    store.close()


def test_run_listing_run_page_and_scene_index(api):
    client, _ = api
    listing = client.get("/api/runs").json()
    assert [r["run"] for r in listing] == [1] and listing[0]["verdicts"] == {"pair": 1, "single": 1}
    page = client.get("/api/runs/1").json()
    assert page["number"] == 1 and {h["id"] for h in page["hulls"]} == {"s__a", "s__b"}
    assert page["image_version"] and page["flags"] == []
    assert client.get("/api/runs/9").status_code == 404
    scenes = client.get("/api/scenes")
    assert [r["scene_id"] for r in scenes.json()] == ["s"]
    assert client.get("/api/scenes", headers={"If-None-Match": scenes.headers["etag"]}).status_code == 304


def test_saving_a_flag_returns_the_stored_row_and_changes_the_run_page_and_etag(api):
    client, store = api
    first = client.get("/api/scenes").headers["etag"]
    before = client.get("/api/runs/1").json()["image_version"]
    saved = client.put("/api/runs/1/flags/s__a", json={"status": "wrong_verdict", "correct_verdict": "single", "note": "tug", "reviewer": "me"})
    assert saved.status_code == 200
    body = saved.json()
    assert (body["hull_id"], body["site"], body["hull"], body["status"], body["final_verdict"]) == ("s__a", "x", "a", "wrong_verdict", "single")
    page = client.get("/api/runs/1").json()
    assert [(f["hull_id"], f["status"], f["note"]) for f in page["flags"]] == [("s__a", "wrong_verdict", "tug")]
    assert {h["id"]: h["final_verdict"] for h in page["hulls"]}["s__a"] == "single"
    assert page["image_version"] != before
    assert client.get("/api/scenes").headers["etag"] != first
    assert store.qa_flags()[0]["reviewer"] == "me"


def test_bad_flags_are_refused_and_store_nothing(api):
    client, store = api
    assert client.put("/api/runs/1/flags/s__zzz", json={"status": "ok"}).status_code == 400
    assert client.put("/api/runs/1/flags/s__a", json={"status": "nonsense"}).status_code == 400
    assert store.qa_flags() == []
