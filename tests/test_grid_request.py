from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.backend import server
from maritime_tip_cue import grid_request, grid_scan, grid_status
from tests.test_grid_scan import RULES, small_grid  # noqa: F401

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
RULES_WITH_STALE = RULES | {"request_stale_minutes": 30}


@pytest.fixture
def api(pgdb, small_grid, monkeypatch):  # noqa: F811
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    monkeypatch.delenv("STS_APP_USER", raising=False)
    monkeypatch.setattr(grid_status, "load_rules", lambda: dict(RULES_WITH_STALE))
    monkeypatch.setattr(grid_status, "load_grid", lambda: small_grid)
    return TestClient(server.app)


def draw(pgdb, batch_utc, tiles, decided=()):
    for position, tile in enumerate(tiles, 1):
        pgdb.execute("INSERT INTO processing.grid_draws VALUES (%s,%s,1,%s)", (batch_utc, tile, position))
    for tile in decided:
        pgdb.execute("INSERT INTO processing.grid_visits VALUES (%s,1,'cold',%s)", (tile, batch_utc))


def state(api):
    return api.get("/api/hotspot-tests/grid/process").json()


def test_first_run_is_number_one_and_starts_a_pending_request(api, pgdb):
    assert state(api) == {
        "action": "start", "run_no": 1, "status": "idle", "requested_by": None,
        "heartbeat_utc": None, "error": None, "decided": None, "tiles": None,
    }
    posted = api.post("/api/hotspot-tests/grid/process", json={"action": "start"})
    assert posted.status_code == 202
    assert posted.json()["action"] is None and posted.json()["status"] == "starting" and posted.json()["run_no"] == 1


def test_a_second_request_while_active_is_refused(api):
    api.post("/api/hotspot-tests/grid/process", json={"action": "start"})
    refused = api.post("/api/hotspot-tests/grid/process", json={"action": "start"})
    assert refused.status_code == 409 and refused.json()["status"] == "starting"


def test_a_finished_run_makes_the_next_one_plus_one(api, pgdb):
    draw(pgdb, NOW, ["T1", "T2"], decided=["T1", "T2"])
    assert state(api)["action"] == "start" and state(api)["run_no"] == 2


def test_an_unfinished_run_offers_continue_with_the_same_number_and_resumes_its_batch(api, pgdb):
    draw(pgdb, NOW, ["T1", "T2"], decided=["T1"])
    current = state(api)
    assert (current["action"], current["run_no"], current["status"], current["decided"], current["tiles"]) == ("continue", 1, "interrupted", 1, 2)
    assert api.post("/api/hotspot-tests/grid/process", json={"action": "start"}).status_code == 409
    assert api.post("/api/hotspot-tests/grid/process", json={"action": "continue"}).status_code == 202
    row = pgdb.execute("SELECT kind, run_no, batch_utc FROM processing.grid_run_requests").fetchone()
    assert (row[0], row[1], row[2]) == ("continue", 1, NOW)


def test_a_stale_running_request_is_interrupted_and_a_pending_one_is_not_picked_up(api, pgdb):
    draw(pgdb, NOW, ["T1", "T2"], decided=["T1"])
    api.post("/api/hotspot-tests/grid/process", json={"action": "continue"})
    pgdb.execute("UPDATE processing.grid_run_requests SET requested_utc=now()-interval '1 hour'")
    assert state(api)["action"] == "continue"
    assert api.post("/api/hotspot-tests/grid/process", json={"action": "continue"}).status_code == 202
    assert [r[0] for r in pgdb.execute("SELECT error FROM processing.grid_run_requests ORDER BY request_id").fetchall()] == ["not picked up", None]
    pgdb.execute("UPDATE processing.grid_run_requests SET status='running', heartbeat_utc=now()-interval '1 hour' WHERE status='pending'")
    assert state(api)["status"] == "interrupted"


def test_run_pending_runs_the_requested_batch_to_the_end_and_a_crash_marks_it_failed(api, pgdb, monkeypatch):
    api.post("/api/hotspot-tests/grid/process", json={"action": "start"})
    seen = {}

    def fake_batch(db, rules, sleep=None, since=None, beat=None):
        seen["since"] = since
        beat()

    monkeypatch.setattr(grid_scan, "batch", fake_batch)
    assert grid_scan.run_pending(pgdb, RULES_WITH_STALE) == "done"
    row = pgdb.execute("SELECT status, started_utc, heartbeat_utc, finished_utc FROM processing.grid_run_requests").fetchone()
    assert row[0] == "done" and None not in row and seen["since"] is not None
    assert grid_scan.run_pending(pgdb, RULES_WITH_STALE) is None

    api.post("/api/hotspot-tests/grid/process", json={"action": "start"})
    monkeypatch.setattr(grid_scan, "batch", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert grid_scan.run_pending(pgdb, RULES_WITH_STALE) == "failed"
    assert pgdb.execute("SELECT status, error FROM processing.grid_run_requests ORDER BY request_id DESC").fetchone() == ("failed", "RuntimeError: boom")


def test_invalid_action_is_rejected(api):
    assert api.post("/api/hotspot-tests/grid/process", json={"action": "stop"}).status_code == 400


def test_a_process_whose_request_was_expired_stops_instead_of_beating_on(api, pgdb, monkeypatch):
    api.post("/api/hotspot-tests/grid/process", json={"action": "start"})

    def batch(db, rules, sleep=None, since=None, beat=None):
        beat()
        pgdb.execute("UPDATE processing.grid_run_requests SET heartbeat_utc=now()-interval '1 hour'")
        grid_request.expire(pgdb.cursor(), RULES_WITH_STALE)
        beat()

    monkeypatch.setattr(grid_scan, "batch", batch)
    assert grid_scan.run_pending(pgdb, RULES_WITH_STALE) == "stopped"
    assert pgdb.execute("SELECT status, error FROM processing.grid_run_requests").fetchone() == ("failed", "interrupted")


def test_run_pending_returns_stopped_when_the_batch_finds_its_request_lost(api, pgdb, monkeypatch):
    api.post("/api/hotspot-tests/grid/process", json={"action": "start"})

    def batch(db, rules, sleep=None, since=None, beat=None):
        pgdb.execute("UPDATE processing.grid_run_requests SET status='failed', error='interrupted'")
        beat()

    monkeypatch.setattr(grid_scan, "batch", batch)
    assert grid_scan.run_pending(pgdb, RULES_WITH_STALE) == "stopped"
    assert pgdb.execute("SELECT status, error FROM processing.grid_run_requests").fetchone() == ("failed", "interrupted")
