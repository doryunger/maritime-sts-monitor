import json

import pytest

from maritime_tip_cue import ledger, worker
from maritime_tip_cue.watch import jobs, registry

JOB = "j" * 64
ATTEMPT = "a" * 32


class S3:
    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    def put_object(self, **kwargs):
        pass


@pytest.fixture
def setup(pgdb, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr(worker, "worker_root", lambda: tmp_path / "worker")
    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"attempt_wait_seconds": 0.3, "attempt_poll_seconds": 0.05, "queue": {"heartbeat_seconds": 60, "lease_seconds": 900}}, "catalogue": {"reviews_prefix": "r/", "results_prefix": "res/"}})
    monkeypatch.setattr(worker, "validate", lambda manifest: None)
    monkeypatch.setattr(worker, "publish_progress", lambda job_id: None)
    monkeypatch.setattr(worker.task_queue, "heartbeat", lambda *a: True)
    seen = []
    saved = []
    real = ledger.ensure_attempt

    def ensure(conn, job_id, attempt_id, owner):
        seen.append(("ensure", conn.execute("select count(*) from review.runs").fetchone()[0]))
        return real(conn, job_id, attempt_id, owner)

    def execute(manifest, store, rules_path):
        row = pgdb.execute(
            "select r.attempt_id, a.job_id, r.origin, r.folder from review.runs r join processing.job_attempts a using (attempt_id) where r.run_key = %s",
            (store.run_key,),
        ).fetchone()
        pgdb.rollback()
        seen.append(("execute", row))
        return {"checked": 1, "failed": 0}

    monkeypatch.setattr(ledger, "ensure_attempt", ensure)
    monkeypatch.setattr(jobs, "execute", execute)
    monkeypatch.setattr(worker, "export_parquets", lambda store, destination, job_id: [])
    monkeypatch.setattr(worker, "upload_artifacts", lambda *a: [])
    monkeypatch.setattr(worker, "attach_review", lambda receipt, *a: receipt.update(review={"prefix": "r/"}))
    monkeypatch.setattr(worker, "publish_pairs", lambda *a: {"pairs": 0})
    monkeypatch.setattr(worker.task_queue, "save_result", lambda job_id, owner, receipt: saved.append(receipt))
    manifest = {"job_id": JOB, "_queue_attempt_id": ATTEMPT, "site": {"name": "site-a"}, "scenes": [{"scene": "S"}], "rules": {}, "rules_sha256": "r" * 64}
    return manifest, seen, saved


def add_job(conn, status="queued"):
    conn.execute(
        "insert into processing.jobs (job_id, site, source, created_utc, manifest_json, manifest_sha256, status) "
        "values (%s, 'x', 's2', now(), '{}', 'x', %s)",
        (JOB, status),
    )
    conn.commit()


def test_the_attempt_is_recorded_before_the_run_row_is_created(pgdb, setup):
    manifest, seen, saved = setup
    add_job(pgdb)
    receipt = worker.run_job(S3(), "b", manifest, queue_owner="w:1")
    assert seen[0] == ("ensure", 0)
    assert seen[1] == ("execute", (ATTEMPT, JOB, "job", "jjjjjjjjjjjj-aaaaaaaa"))
    assert receipt["status"] == "succeeded" and receipt["run_no"] == 1 and saved == [receipt]
    assert pgdb.execute("select status, current_attempt_id from processing.jobs").fetchone() == ("running", ATTEMPT)
    assert json.loads((worker.worker_root() / JOB / "run.json").read_bytes()) == {"run_no": 1}
    pgdb.rollback()


def test_an_attempt_the_manager_recorded_first_is_used_as_it_is(pgdb, setup):
    manifest, seen, _ = setup
    add_job(pgdb, "running")
    pgdb.execute(
        "insert into processing.job_attempts (attempt_id, job_id, attempt_no, owner, status, started_utc) values (%s, %s, 1, 'manager', 'running', now())",
        (ATTEMPT, JOB),
    )
    pgdb.commit()
    assert worker.run_job(S3(), "b", manifest, queue_owner="w:1")["status"] == "succeeded"
    assert pgdb.execute("select owner from processing.job_attempts").fetchall() == [("manager",)]
    assert seen[1][1][0] == ATTEMPT


def test_a_second_run_of_the_same_attempt_reuses_its_run(pgdb, setup):
    manifest, seen, _ = setup
    add_job(pgdb)
    worker.run_job(S3(), "b", manifest, queue_owner="w:1")
    second = worker.run_job(S3(), "b", manifest, queue_owner="w:1")
    assert second["run_no"] == 1 and pgdb.execute("select count(*) from review.runs").fetchone()[0] == 1


def test_no_run_is_created_when_the_attempt_never_appears(pgdb, setup):
    manifest, seen, saved = setup
    with pytest.raises(RuntimeError, match="not in the job ledger"):
        worker.run_job(S3(), "b", manifest, queue_owner="w:1")
    assert pgdb.execute("select count(*) from review.runs").fetchone()[0] == 0
    assert [name for name, _ in seen] == ["ensure"] and saved == []


def test_a_run_for_a_job_that_cannot_take_an_attempt_waits_then_fails(pgdb, setup):
    manifest, seen, _ = setup
    add_job(pgdb, "succeeded")
    with pytest.raises(RuntimeError, match="not in the job ledger"):
        worker.run_job(S3(), "b", manifest, queue_owner="w:1")
    assert pgdb.execute("select count(*) from review.runs").fetchone()[0] == 0
