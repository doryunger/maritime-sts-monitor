import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backend import auth, server


@pytest.fixture
def api(pgdb, monkeypatch):
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    monkeypatch.delenv("STS_APP_USER", raising=False)
    return TestClient(server.app)


def test_idle_state_has_nothing(api):
    assert api.get("/api/sync").json() == {"latest": None, "active": None, "last_finished": None}


def test_post_creates_a_pending_request_and_a_second_is_refused_with_the_current_one(api, pgdb):
    first = api.post("/api/sync")
    assert first.status_code == 202
    body = first.json()
    assert body["active"]["status"] == "pending" and body["active"]["requested_by"] == "local" and body["active"]["dry_run"] is False
    second = api.post("/api/sync")
    assert second.status_code == 409 and second.json()["active"]["sync_id"] == body["active"]["sync_id"]
    assert pgdb.execute("select count(*) from review.sync_runs").fetchone()[0] == 1
    pgdb.execute("update review.sync_runs set status = 'running', started_utc = now()")
    pgdb.commit()
    assert api.post("/api/sync").status_code == 409


def test_get_reports_latest_and_last_finished_with_summary(api, pgdb):
    pgdb.execute(
        "insert into review.sync_runs (requested_by, status, finished_utc, summary) values ('a', 'done', now(), '{\"totals\": {\"added\": 2}}'), "
        "('b', 'failed', now(), null)"
    )
    pgdb.commit()
    api.post("/api/sync")
    state = api.get("/api/sync").json()
    assert state["latest"]["status"] == "pending" and state["active"]["requested_by"] == "local"
    assert state["last_finished"]["requested_by"] == "b" and state["last_finished"]["status"] == "failed"
    pgdb.execute("delete from review.sync_runs where requested_by = 'b'")
    pgdb.commit()
    assert api.get("/api/sync").json()["last_finished"]["summary"]["totals"]["added"] == 2


def test_requested_by_is_the_authenticated_user_and_both_routes_need_login(pgdb, monkeypatch):
    auth._failures.clear()
    monkeypatch.setenv("STS_APP_PASSWORD", "s3cret")
    monkeypatch.setenv("STS_APP_USER", "dor")
    guarded = FastAPI()
    auth.install(guarded)
    guarded.add_api_route("/api/sync", server.get_sync)
    guarded.add_api_route("/api/sync", server.post_sync, methods=["POST"])
    client = TestClient(guarded)
    assert client.get("/api/sync").status_code == 401
    assert client.post("/api/sync").status_code == 401
    assert pgdb.execute("select count(*) from review.sync_runs").fetchone()[0] == 0
    headers = {"Authorization": "Basic " + base64.b64encode(b"dor:s3cret").decode()}
    assert client.post("/api/sync", headers=headers).json()["active"]["requested_by"] == "dor"
