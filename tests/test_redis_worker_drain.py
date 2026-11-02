import importlib
import sys
from types import SimpleNamespace

try:
    import redis  # noqa: F401
except ImportError:
    sys.modules["redis"] = SimpleNamespace(Redis=object)

worker = importlib.import_module("maritime_tip_cue.worker")


def test_drain_attempts_each_job_once_and_picks_up_jobs_queued_while_it_runs(monkeypatch):
    jobs = {
        "failed-job": {"job_id": "failed-job", "site": {"name": "site-a"}},
        "good-job": {"job_id": "good-job", "site": {"name": "site-b"}},
    }
    claimed = []
    monkeypatch.setattr(worker.task_queue, "ready_ids", lambda: list(jobs))

    def claim(job_id, owner):
        claimed.append(job_id)
        return jobs[job_id]

    def run_job(client, bucket, manifest, queue_owner):
        if manifest["job_id"] == "failed-job":
            return {"status": "failed", "run_no": 1, "error": "retry later"}
        return {"status": "succeeded", "run_no": 2, "error": None}

    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"queue": {"max_seconds_per_scene": 1200}}})
    monkeypatch.setattr(worker.task_queue, "claim", claim)
    monkeypatch.setattr(worker, "run_job", run_job)

    results = worker.drain_jobs(object(), "bucket")

    assert claimed == ["failed-job", "good-job"]
    assert [row["status"] for row in results] == ["failed", "succeeded"]


def test_drain_ends_only_when_nothing_new_is_ready(monkeypatch):
    queue = ["first"]
    ran = []
    monkeypatch.setattr(worker.task_queue, "ready_ids", lambda: list(queue))
    monkeypatch.setattr(worker.task_queue, "claim", lambda job_id, owner: {"job_id": job_id, "site": {"name": "s"}})
    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"queue": {"max_seconds_per_scene": 1200}}})

    def run_job(client, bucket, manifest, queue_owner):
        ran.append(manifest["job_id"])
        if manifest["job_id"] == "first":
            queue.append("added-meanwhile")
        return {"status": "succeeded", "run_no": len(ran), "error": None}

    monkeypatch.setattr(worker, "run_job", run_job)
    assert len(worker.drain_jobs(object(), "bucket")) == 2
    assert ran == ["first", "added-meanwhile"]


def test_drain_stops_claiming_once_the_session_budget_is_spent_and_names_its_claims(monkeypatch):
    jobs = {f"job-{i}": {"job_id": f"job-{i}", "site": {"name": "site"}} for i in range(3)}
    owners = []
    clock = {"now": 0.0}
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(worker.task_queue, "ready_ids", lambda: list(jobs))
    monkeypatch.setattr(worker, "settings", lambda: {"worker": {"queue": {"max_seconds_per_scene": 1200}}})

    def claim(job_id, owner):
        owners.append(owner)
        return jobs[job_id]

    def run_job(client, bucket, manifest, queue_owner):
        clock["now"] += 600
        return {"status": "succeeded", "run_no": 1, "error": None}

    monkeypatch.setattr(worker.task_queue, "claim", claim)
    monkeypatch.setattr(worker, "run_job", run_job)
    results = worker.drain_jobs(object(), "bucket", stop_after=900, session="drain-7")
    assert [row["job_id"] for row in results] == ["job-0", "job-1"]
    assert all(owner.startswith("drain-7:") for owner in owners)
