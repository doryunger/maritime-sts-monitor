import json
import threading
from contextlib import closing

import fakeredis
import pytest

from maritime_tip_cue import catalogue, config, task_queue, worker

QUEUE = {"lease_seconds": 900, "heartbeat_seconds": 1, "max_retries": 1, "retry_delay_seconds": 300}


@pytest.fixture
def redis(monkeypatch):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    monkeypatch.setattr(config, "settings", lambda: {"worker": {"queue": dict(QUEUE)}})
    return server


def manifest(job_id):
    return {"job_id": job_id, "site": {"name": "site-a"}, "scenes": [{"scene": "s"}]}


def test_requeued_failed_job_can_be_dispatched_and_claimed_again(redis, tmp_path):
    with closing(catalogue.connect(tmp_path / "t.db")) as db:
        db.execute(
            "INSERT INTO jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status,retry_count,error) "
            "VALUES ('j','site-a','s2','t',?,'j','failed',2,'boom')",
            (json.dumps(manifest("j")),),
        )
        db.commit()
        task_queue.restore({"j": manifest("j")}, [])
        task_queue.claim("j", "w1")
        task_queue.finalize("j", "w1", "failed")
        assert task_queue.claim("j", "w2") is None
        assert catalogue.requeue_failed(db)["requeued"] == ["j"]
        catalogue.refresh_queue(db)
        row = db.execute("SELECT status,retry_count,error FROM jobs WHERE job_id='j'").fetchone()
        assert tuple(row) == ("dispatched", 0, None)
    assert task_queue.claim("j", "w2") is not None


def run_with_abort(monkeypatch, heartbeat, per_scene=None, lease=900):
    exits = []
    released = threading.Event()
    cfg = dict(QUEUE, lease_seconds=lease)
    if per_scene:
        cfg["max_seconds_per_scene"] = per_scene
    monkeypatch.setattr(config, "settings", lambda: {"worker": {"queue": cfg}})
    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"queue": cfg}})
    monkeypatch.setattr(worker.task_queue, "heartbeat", heartbeat)

    def fake_exit(code):
        exits.append(code)
        released.set()

    monkeypatch.setattr(worker.os, "_exit", fake_exit)

    def slow_job(*args, **kwargs):
        released.wait(10)
        return {"status": "succeeded"}

    monkeypatch.setattr(worker, "_run_job", slow_job)
    worker.run_job(object(), "bucket", {"job_id": "j", "_queue_attempt_id": "a", "scenes": [1]}, queue_owner="w")
    return exits


def test_lost_lease_aborts_the_attempt(redis, monkeypatch):
    assert run_with_abort(monkeypatch, lambda *a: False) == [75]


def test_unreachable_redis_aborts_only_after_the_lease_would_have_expired(redis, monkeypatch):
    def broken(*args):
        raise ConnectionError("down")

    assert run_with_abort(monkeypatch, broken, lease=2) == [75]


def test_runtime_limit_aborts_hung_job_even_with_healthy_heartbeat(redis, monkeypatch):
    assert run_with_abort(monkeypatch, lambda *a: True, per_scene=1) == [75]


def test_no_runtime_limit_when_unset(redis, monkeypatch):
    released = threading.Event()
    threading.Timer(2.5, released.set).start()
    exits = []
    cfg = dict(QUEUE)
    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"queue": cfg}})
    monkeypatch.setattr(worker.task_queue, "heartbeat", lambda *a: True)
    monkeypatch.setattr(worker.os, "_exit", exits.append)
    monkeypatch.setattr(worker, "_run_job", lambda *a, **k: released.wait(10) and {"status": "succeeded"})
    worker.run_job(object(), "bucket", {"job_id": "j", "_queue_attempt_id": "a", "scenes": [1]}, queue_owner="w")
    assert exits == []


def test_drain_refuses_to_run_without_a_runtime_limit(redis, monkeypatch):
    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"queue": dict(QUEUE)}})
    with pytest.raises(RuntimeError, match="max_seconds_per_scene"):
        worker.drain_jobs(object(), "bucket")


def test_publish_failure_fails_the_attempt_instead_of_hiding_missing_findings(monkeypatch, tmp_path):
    from maritime_tip_cue import publish

    def boom(*args):
        raise PermissionError("AccessDenied")

    monkeypatch.setattr(publish, "publish_run", boom)
    monkeypatch.setattr("maritime_tip_cue.sts.rules.load", lambda path: {})
    with pytest.raises(RuntimeError, match="publish failed: PermissionError: AccessDenied"):
        worker.publish_pairs(None, "b", tmp_path, 1, "J", tmp_path / "rules.json")
