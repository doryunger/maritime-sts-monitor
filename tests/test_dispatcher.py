import importlib
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fcntl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "catalogue"))
dispatch = importlib.import_module("dispatch_worker")
worker_host = importlib.import_module("worker_host")

CFG = {"max_session_seconds": 3600, "max_no_progress_sessions": 3, "max_seconds_per_day": 7200, "settle_seconds": 60}


class Queue:
    def __init__(self, ready=2, retry_wait=0, completed=0):
        self.counts = {"ready": ready, "retry_wait": retry_wait, "claimed": 0, "completed": completed, "failed": 0}

    def __call__(self):
        return dict(self.counts)


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value


def session(tmp_path, queue, worker, clock, **kwargs):
    return dispatch.run_session(tmp_path, queue, worker, CFG, clock=clock, sleep=lambda s: None, **kwargs)


def test_empty_queue_never_wakes_worker(tmp_path):
    calls = []
    result = session(tmp_path, Queue(ready=0), lambda s: calls.append(s) or 0, Clock())
    assert result["action"] == "skip" and calls == []


def test_only_delayed_retries_never_wakes_worker(tmp_path):
    calls = []
    result = session(tmp_path, Queue(ready=0, retry_wait=3), lambda s: calls.append(s) or 0, Clock())
    assert result["reason"] == "no ready jobs" and calls == []


def test_pause_flag_blocks_wake(tmp_path):
    (tmp_path / "paused").touch()
    calls = []
    result = session(tmp_path, Queue(), lambda s: calls.append(s) or 0, Clock())
    assert result["reason"] == "paused" and calls == []


def test_dry_run_decides_without_waking_or_writing_state(tmp_path):
    calls = []
    result = session(tmp_path, Queue(), lambda s: calls.append(s) or 0, Clock(), dry_run=True)
    assert result["action"] == "would-run" and calls == []
    assert not (tmp_path / "state.json").exists()


def test_session_is_bounded_and_records_progress(tmp_path):
    queue, clock, seen = Queue(), Clock(), []

    def worker(max_seconds):
        seen.append(max_seconds)
        queue.counts["completed"] += 2
        clock.value += 600
        return 0

    result = session(tmp_path, queue, worker, clock)
    assert seen == [3600]
    assert result["progress"] == 2 and result["seconds"] == 600 and result["no_progress_sessions"] == 0


def test_breaker_opens_after_repeated_sessions_without_progress(tmp_path):
    queue, clock, calls = Queue(), Clock(), []

    def worker(max_seconds):
        calls.append(max_seconds)
        clock.value += 1000
        return 75

    for _ in range(3):
        assert session(tmp_path, queue, worker, clock)["action"] == "ran"
    blocked = session(tmp_path, queue, worker, clock)
    assert blocked["action"] == "skip" and "breaker" in blocked["reason"] and len(calls) == 3
    state = dispatch.load_state(tmp_path)
    state["no_progress_sessions"] = 0
    dispatch.save_state(tmp_path, state)
    assert session(tmp_path, queue, worker, clock)["action"] == "ran"


def test_progress_resets_breaker(tmp_path):
    queue, clock = Queue(), Clock()

    def stuck(max_seconds):
        clock.value += 1000
        return 1

    def good(max_seconds):
        queue.counts["completed"] += 1
        clock.value += 1000
        return 0

    session(tmp_path, queue, stuck, clock)
    session(tmp_path, queue, stuck, clock)
    assert session(tmp_path, queue, good, clock)["no_progress_sessions"] == 0


def test_daily_worker_seconds_cap(tmp_path):
    queue, clock, calls = Queue(), Clock(), []

    def worker(max_seconds):
        calls.append(1)
        queue.counts["completed"] += 1
        clock.value += 3600
        return 0

    session(tmp_path, queue, worker, clock)
    session(tmp_path, queue, worker, clock)
    blocked = session(tmp_path, queue, worker, clock)
    assert blocked["reason"] == "daily worker-seconds cap reached" and len(calls) == 2
    clock.value += 86400
    assert session(tmp_path, queue, worker, clock)["action"] == "ran"


def test_second_dispatcher_cannot_run_concurrently(tmp_path):
    import fcntl

    tmp_path.mkdir(exist_ok=True)
    with (tmp_path / "dispatcher.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = []
        result = session(tmp_path, Queue(), lambda s: calls.append(s) or 0, Clock())
    assert "another dispatcher" in result["reason"] and calls == []


def test_remote_command_has_runtime_limit_and_container_cleanup():
    settings = {"image": "img:1", "remote_dir": "/srv/w"}
    command = worker_host.remote_worker_command(settings, "drain", "sts-worker-drain", 3600)
    assert "timeout -k 60 3600 docker run" in command
    assert command.count("docker rm -f sts-worker-drain") == 2
    assert command.endswith("exit $code")


def test_hung_session_is_terminated_and_reported_as_timeout(monkeypatch):
    events = []

    class Hung:
        def wait(self, timeout=None):
            if "terminated" not in events:
                raise subprocess.TimeoutExpired("x", timeout)
            return -15

        def terminate(self):
            events.append("terminated")

    monkeypatch.setattr(worker_host.subprocess, "Popen", lambda *a, **k: Hung())
    monkeypatch.setattr(worker_host.shutil, "which", lambda name: "bash")
    settings = {"key_path": Path("k"), "poll_seconds": "1", "ready_timeout": 10, "status_url": "u", "private_ip": "i"}
    assert worker_host.run_remote(settings, "cmd", session_timeout=5) == 124
    assert events == ["terminated"]


def test_waits_for_manager_to_record_the_last_result_before_judging_progress(tmp_path):
    queue, clock = Queue(), Clock()
    reads = {"after": 0}

    def counts():
        data = dict(queue.counts)
        if queue.counts["claimed"] and reads["after"]:
            reads["after"] += 1
            if reads["after"] == 3:
                queue.counts["claimed"] = 0
                queue.counts["completed"] += 1
        return data

    def worker(max_seconds):
        queue.counts["claimed"] = 1
        reads["after"] = 1
        clock.value += 100
        return 0

    result = dispatch.run_session(tmp_path, counts, worker, CFG, clock=clock, sleep=lambda s: None)
    assert result["progress"] == 1 and result["no_progress_sessions"] == 0


def test_does_not_wake_while_jobs_are_claimed_elsewhere(tmp_path):
    queue, calls = Queue(), []
    queue.counts["claimed"] = 1
    result = session(tmp_path, queue, lambda s: calls.append(s) or 0, Clock())
    assert result["reason"] == "jobs are being processed elsewhere" and calls == []
