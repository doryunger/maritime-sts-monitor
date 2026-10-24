import json
from contextlib import closing

import fakeredis
import pytest

from maritime_tip_cue import catalogue, job_manager, task_queue


def scenes(*names):
    return {"scenes": [{"site": "s", "source": "s2", "scene": n} for n in names], "site": {"name": "s"}}


@pytest.fixture
def db(tmp_path):
    with closing(catalogue.connect(tmp_path / "t.db")) as connection:
        yield connection


def add(db, job_id, status, created, *names):
    db.execute(
        "INSERT INTO jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) VALUES (?,?,?,?,?,?,?)",
        (job_id, "s", "s2", created, json.dumps(scenes(*names)), job_id, status),
    )
    db.commit()


def status(db, job_id):
    row = db.execute("SELECT status,superseded_by FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return tuple(row)


def test_rules_change_twin_keeps_only_the_newest_open_job(db):
    add(db, "old", "dispatched", "2026-10-21", "a")
    add(db, "new", "dispatched", "2026-10-22", "a")
    assert catalogue.supersede_jobs(db) == {"old": "new"}
    assert status(db, "old") == ("superseded", "new") and status(db, "new")[0] == "dispatched"


def test_failed_job_for_a_scene_a_later_job_completed_is_closed_not_rerun(db):
    add(db, "failed1", "failed", "2026-10-20", "a")
    add(db, "failed2", "failed", "2026-10-20", "a")
    add(db, "done", "succeeded", "2026-10-21", "a")
    catalogue.supersede_jobs(db)
    assert status(db, "failed1") == ("superseded", "done") and status(db, "failed2") == ("superseded", "done")
    assert status(db, "done")[0] == "succeeded"


def test_open_job_for_an_already_succeeded_scene_is_superseded(db):
    add(db, "done", "succeeded", "2026-10-21", "a")
    add(db, "open", "pending", "2026-10-22", "a")
    catalogue.supersede_jobs(db)
    assert status(db, "open") == ("superseded", "done")


def test_failed_job_with_an_open_twin_is_closed_but_unrelated_failure_stays(db):
    add(db, "failed", "failed", "2026-10-20", "a")
    add(db, "open", "dispatched", "2026-10-22", "a")
    add(db, "other", "failed", "2026-10-20", "b")
    catalogue.supersede_jobs(db)
    assert status(db, "failed") == ("superseded", "open")
    assert status(db, "other") == ("failed", None)


def test_running_and_partially_overlapping_jobs_are_left_alone(db):
    add(db, "running", "running", "2026-10-22", "a")
    add(db, "done", "succeeded", "2026-10-21", "a")
    add(db, "partial", "pending", "2026-10-22", "a", "b")
    assert catalogue.supersede_jobs(db) == {}


def test_superseded_job_is_removed_from_redis_and_not_requeued(db, monkeypatch):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    add(db, "old", "dispatched", "2026-10-21", "a")
    add(db, "new", "dispatched", "2026-10-22", "a")
    catalogue.refresh_queue(db)
    assert server.zcard(task_queue.keys()["pending"]) == 2
    catalogue.supersede_jobs(db)
    task_queue.drop([r[0] for r in db.execute("SELECT job_id FROM jobs WHERE status='superseded'")])
    assert server.zrange(task_queue.keys()["pending"], 0, -1) == ["new"]
    catalogue.refresh_queue(db)
    assert server.zrange(task_queue.keys()["pending"], 0, -1) == ["new"]
    assert hasattr(job_manager, "cycle")


def observe(db, scene, first="2026-10-22"):
    db.execute(
        "INSERT INTO observations VALUES ('s','s2',?,'v','g',?,?,'{}')",
        (scene, first, first),
    )
    db.commit()


def test_audit_classifies_every_scene_and_flags_gaps(db):
    db.execute("INSERT INTO channels VALUES ('s','s2','2026-10-20')")
    for scene in "abcdefg":
        observe(db, scene, "2026-10-20" if scene == "g" else "2026-10-22")
    add(db, "j1", "succeeded", "2026-10-22", "a")
    add(db, "j2", "pending", "2026-10-22", "b")
    add(db, "j3", "failed", "2026-10-22", "c")
    add(db, "j4", "running", "2026-10-22", "d")
    add(db, "j5", "superseded", "2026-10-22", "e")
    report = catalogue.audit(db)
    assert report["scenes"]["s/s2"] == {
        "completed": 1, "queued": 1, "failed": 1, "running": 1, "superseded_only": 1, "orphan": 1, "baseline": 1
    }
    problems = sorted(v["problem"] for v in report["violations"])
    assert problems == ["every job for it is superseded", "observed after baseline but in no job"]


def test_audit_flags_completed_scene_still_open_and_redis_drift(db, monkeypatch):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    add(db, "done", "succeeded", "2026-10-21", "a")
    add(db, "open", "pending", "2026-10-22", "a")
    server.zadd(task_queue.keys()["pending"], {"ghost": 0})
    report = catalogue.audit(db, server)
    assert any(v["problem"] == "completed but still in an open job" for v in report["violations"])
    assert report["redis_drift"]["pending"] == {"in_redis_only": ["ghost"], "in_ledger_only": ["open"]}
    assert report["redis_drift"]["completed"] == {"in_redis_only": [], "in_ledger_only": ["done"]}
