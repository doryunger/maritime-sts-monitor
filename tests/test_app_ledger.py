import json
from contextlib import closing

import fakeredis
import pytest

from app.backend import ledger, queue
from maritime_tip_cue import catalogue, config, task_queue

JOB = "a" * 64


@pytest.fixture
def local_ledger(monkeypatch, tmp_path):
    database = tmp_path / "tracker.db"
    manifest = {
        "job_id": JOB,
        "site": {"name": "site-a"},
        "scenes": [{"site": "site-a", "source": "s2", "scene": "S2B_X_T40REN_Y", "time": "2026-10-16T06:52:00Z"}],
    }
    with closing(catalogue.connect(database)) as db:
        db.execute(
            "INSERT INTO jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) VALUES (?,?,?,?,?,?,?)",
            (JOB, "site-a", "s2", "2026-10-22T06:05:00+00:00", json.dumps(manifest), JOB, "running"),
        )
        db.commit()
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    monkeypatch.setattr(config, "settings", lambda: {"catalogue": {"database": str(database)}})
    monkeypatch.setenv("STS_LEDGER_LOCAL", "1")
    ledger._cache.update(at=0.0, value=None)
    return server


def test_local_mode_reads_the_ledger_file_without_ssh(local_ledger, monkeypatch):
    monkeypatch.setattr(ledger, "control_host", lambda: (_ for _ in ()).throw(AssertionError("ssh must not be used")))
    jobs = ledger.fetch()["jobs"]
    assert [(j["job_id"], j["status"]) for j in jobs] == [(JOB, "running")]


def test_snapshot_shows_live_scene_progress_from_redis(local_ledger):
    task_queue.save_progress(JOB, {"S2B_X_T40REN_Y": {"status": "detected", "stage": "pair", "updated_utc": "t"}})
    ledger._cache.update(at=0.0, value=None)
    snapshot = queue.snapshot()
    job = snapshot["jobs"][0]
    assert snapshot["error"] is None and job["status"] == "claimed"
    assert job["scene_details"][0]["state"] == "detected"


def test_unreadable_ledger_reports_an_error_not_an_empty_list(local_ledger, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "settings", lambda: {"catalogue": {"database": str(tmp_path / "missing.db")}})
    ledger._cache.update(at=0.0, value=None)
    snapshot = queue.snapshot()
    assert snapshot["jobs"] is None and snapshot["error"]
