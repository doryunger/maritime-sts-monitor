import fakeredis
import pytest
from psycopg.types.json import Jsonb

from app.backend import ledger, queue
from maritime_tip_cue import task_queue

JOB = "a" * 64


@pytest.fixture
def local_ledger(monkeypatch, pgdb):
    manifest = {
        "job_id": JOB,
        "site": {"name": "site-a"},
        "scenes": [{"site": "site-a", "source": "s2", "scene": "S2B_X_T40REN_Y", "time": "2026-10-16T06:52:00Z"}],
    }
    pgdb.execute(
        "INSERT INTO processing.jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (JOB, "site-a", "s2", "2026-10-22T06:05:00+00:00", Jsonb(manifest), JOB, "running"),
    )
    pgdb.commit()
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    ledger._cache.update(at=0.0, value=None)
    return server


def test_the_ledger_is_read_from_postgres(local_ledger):
    jobs = ledger.fetch()["jobs"]
    assert [(j["job_id"], j["status"]) for j in jobs] == [(JOB, "running")]
    assert jobs[0]["created_utc"] == "2026-10-22T06:05:00+00:00"
    assert jobs[0]["scenes"] == [{"scene_id": "S2B_X_T40REN_Y", "source": "s2", "captured": "2026-10-16T06:52:00Z"}]


def test_snapshot_shows_live_scene_progress_from_redis(local_ledger):
    task_queue.save_progress(JOB, {"S2B_X_T40REN_Y": {"status": "detected", "stage": "pair", "updated_utc": "t"}})
    ledger._cache.update(at=0.0, value=None)
    snapshot = queue.snapshot()
    job = snapshot["jobs"][0]
    assert snapshot["error"] is None and job["status"] == "claimed"
    assert job["scene_details"][0]["state"] == "detected"
    assert job["last_update_utc"] == "2026-10-22T06:05:00+00:00"


def test_unreadable_ledger_reports_an_error_not_an_empty_list(local_ledger, monkeypatch):
    monkeypatch.setenv("STS_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    ledger._cache.update(at=0.0, value=None)
    snapshot = queue.snapshot()
    assert snapshot["jobs"] is None and snapshot["error"]
