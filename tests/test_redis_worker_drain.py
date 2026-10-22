import importlib
import sys
from types import SimpleNamespace

try:
    import redis  # noqa: F401
except ImportError:
    sys.modules["redis"] = SimpleNamespace(Redis=object)

worker = importlib.import_module("maritime_tip_cue.worker")


def test_drain_attempts_each_job_from_initial_snapshot_once(monkeypatch):
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
