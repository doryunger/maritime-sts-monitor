
import fakeredis
import pytest

from maritime_tip_cue import publish, task_queue
from maritime_tip_cue.watch.state import Store

JOB = "a" * 64


@pytest.fixture
def runs(monkeypatch, tmp_path):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    folders = {}
    monkeypatch.setattr(publish.registry, "folder_of", lambda no: folders[no])

    def make(no, status, job_id=None):
        folder = tmp_path / str(no)
        folder.mkdir()
        folders[no] = folder
        store = Store(folder / "state.db", folder)
        run_id = store.start_run({"job_id": job_id} if job_id else {}, "site", ("a", "b"), "s2", "r", "c")
        store.finish_run(run_id, status)
        store.close()

    return server, make


def test_publishes_only_runs_whose_job_succeeded_in_the_ledger(runs):
    server, make = runs
    make(1, "done", JOB)
    server.sadd(task_queue.keys()["completed"], JOB)
    publish.check_runs([1])


def test_refuses_run_whose_job_is_not_completed(runs):
    _, make = runs
    make(1, "done", JOB)
    with pytest.raises(SystemExit, match="no succeeded attempt"):
        publish.check_runs([1])


@pytest.mark.parametrize("status", ["failed", "running"])
def test_refuses_unfinished_or_failed_runs(runs, status):
    server, make = runs
    make(1, status, JOB)
    server.sadd(task_queue.keys()["completed"], JOB)
    with pytest.raises(SystemExit, match=f"run status is {status}"):
        publish.check_runs([1])


def test_legacy_run_needs_explicit_flag_and_one_bad_run_blocks_all(runs):
    server, make = runs
    make(1, "done")
    make(2, "done", JOB)
    server.sadd(task_queue.keys()["completed"], JOB)
    with pytest.raises(SystemExit, match="run 1: not created by a queue job"):
        publish.check_runs([1, 2])
    publish.check_runs([1, 2], allow_legacy=True)
