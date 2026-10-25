import json

import fakeredis
import pytest
from psycopg.types.json import Jsonb

from maritime_tip_cue import catalogue, task_queue

JOB = "j" * 64


@pytest.fixture
def setup(monkeypatch, pgdb):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    manifest = {"job_id": JOB, "site": {"name": "site-a"}, "scenes": [{"scene": "s"}]}
    pgdb.execute(
        "INSERT INTO processing.jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) "
        "VALUES (%s,%s,%s,now(),%s,%s,%s)",
        (JOB, "site-a", "s2", Jsonb(manifest), JOB, "dispatched"),
    )
    pgdb.commit()
    return pgdb, server, manifest


def attempt_events(server, attempt, kind):
    return [
        ("1-0", {"type": "claimed", "job_id": JOB, "attempt_id": attempt, "owner": "w"}),
        ("2-0", {"type": kind, "job_id": JOB, "attempt_id": attempt, "owner": "w"}),
    ]


def receipt(attempt, status, error=None):
    return {
        "schema_version": 1,
        "job_id": JOB,
        "attempt_id": attempt,
        "manifest_sha256": JOB,
        "status": status,
        "artifacts": [],
        "scene_counts": {"checked": 0, "failed": 1},
        "error": error,
    }


def reconcile(db, server, events):
    return catalogue.reconcile_redis(db, server, None, "bucket", "catalogue/artifacts/", events, 1, 300)


def test_stopped_receipt_is_recorded_as_stopped_and_retried(setup):
    db, server, _ = setup
    task_queue.restore({JOB: db.execute("select manifest_json from processing.jobs").fetchone()[0]}, [])
    server.set(task_queue.keys()["result"] + JOB + ":a1", json.dumps(receipt("a1", "stopped", "maximum runtime")))
    reconcile(db, server, attempt_events(server, "a1", "result"))
    status = catalogue.job_status(db, JOB)
    assert status["status"] == "retry_wait"
    assert [a["verdict"] for a in status["attempts"]] == ["stopped"]
    assert status["attempts"][0]["error"] == "maximum runtime"


def test_lease_expiry_without_receipt_is_also_stopped(setup):
    db, server, _ = setup
    reconcile(db, server, attempt_events(server, "a1", "lease_expired"))
    status = catalogue.job_status(db, JOB)
    assert [a["verdict"] for a in status["attempts"]] == ["stopped"]
    assert "without a receipt" in status["attempts"][0]["error"]


def test_failed_receipt_is_failed_and_second_failure_is_terminal(setup):
    db, server, _ = setup
    for attempt in ("a1", "a2"):
        server.set(task_queue.keys()["result"] + JOB + ":" + attempt, json.dumps(receipt(attempt, "failed", "boom")))
        reconcile(db, server, attempt_events(server, attempt, "result"))
    status = catalogue.job_status(db, JOB)
    assert status["status"] == "failed"
    assert [a["verdict"] for a in status["attempts"]] == ["failed", "failed"]
