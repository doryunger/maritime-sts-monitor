import importlib
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fcntl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "catalogue"))
dispatch = importlib.import_module("dispatch_worker")
worker_host = importlib.import_module("worker_host")

CFG = {"kill_grace_seconds": 600, "min_session_seconds": 900, "max_no_progress_sessions": 3, "max_seconds_per_day": 7200, "settle_seconds": 60, "defer_backoff_seconds": 900}


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
    return dispatch.run_session(tmp_path, queue, worker, CFG, clock=clock, sleep=lambda s, *_: None, **kwargs)


def test_empty_queue_never_wakes_worker(tmp_path):
    calls = []
    result = session(tmp_path, Queue(ready=0), lambda s, *_: calls.append(s) or 0, Clock())
    assert result["action"] == "skip" and calls == []


def test_only_delayed_retries_never_wakes_worker(tmp_path):
    calls = []
    result = session(tmp_path, Queue(ready=0, retry_wait=3), lambda s, *_: calls.append(s) or 0, Clock())
    assert result["reason"] == "no ready jobs" and calls == []


def test_pause_flag_blocks_wake(tmp_path):
    (tmp_path / "paused").touch()
    calls = []
    result = session(tmp_path, Queue(), lambda s, *_: calls.append(s) or 0, Clock())
    assert result["reason"] == "paused" and calls == []


def test_dry_run_decides_without_waking_or_writing_state(tmp_path):
    calls = []
    result = session(tmp_path, Queue(), lambda s, *_: calls.append(s) or 0, Clock(), dry_run=True)
    assert result["action"] == "would-run" and calls == []
    assert not (tmp_path / "state.json").exists()


def test_session_budget_is_what_is_left_of_the_day(tmp_path):
    queue, clock, seen = Queue(), Clock(), []

    def worker(max_seconds, *_):
        seen.append(max_seconds)
        queue.counts["completed"] += 2
        clock.value += 600
        return 0

    result = session(tmp_path, queue, worker, clock)
    assert seen == [7200]
    assert result["progress"] == 2 and result["seconds"] == 600 and result["no_progress_sessions"] == 0


def test_second_session_gets_only_the_remaining_budget_and_a_session_name(tmp_path):
    queue, clock, seen = Queue(), Clock(), []

    def worker(budget, name, grace):
        seen.append((budget, name, grace))
        queue.counts["completed"] += 1
        clock.value += 3000
        return 0

    session(tmp_path, queue, worker, clock)
    session(tmp_path, queue, worker, clock)
    assert [(budget, grace) for budget, _, grace in seen] == [(7200, 600), (4200, 600)]
    assert seen[0][1] != seen[1][1] and seen[0][1].startswith("drain-")


def test_a_day_with_less_than_the_minimum_left_does_not_wake_the_worker(tmp_path):
    queue, clock, calls = Queue(), Clock(), []

    def worker(*args):
        calls.append(args)
        queue.counts["completed"] += 1
        clock.value += 6500
        return 0

    session(tmp_path, queue, worker, clock)
    blocked = session(tmp_path, queue, worker, clock)
    assert blocked["reason"] == "daily worker-seconds cap reached" and len(calls) == 1


def test_claims_of_a_session_that_did_not_exit_cleanly_are_released_before_settling(tmp_path):
    queue, clock, released = Queue(), Clock(), []

    def killed(budget, name, grace):
        queue.counts["claimed"] = 1
        clock.value += 100
        return 137

    def release(name):
        released.append(name)
        queue.counts["claimed"] = 0

    result = dispatch.run_session(tmp_path, queue, killed, CFG, clock=clock, sleep=lambda s: None, release=release)
    assert len(released) == 1 and released[0].startswith("drain-") and result["queue"]["claimed"] == 0


def test_a_clean_session_releases_nothing(tmp_path):
    queue, clock, released = Queue(), Clock(), []
    dispatch.run_session(tmp_path, queue, lambda *a: 0, CFG, clock=clock, sleep=lambda s: None, release=released.append)
    assert released == []


def test_breaker_opens_after_repeated_sessions_without_progress(tmp_path):
    queue, clock, calls = Queue(), Clock(), []

    def worker(max_seconds, *_):
        calls.append(max_seconds)
        clock.value += 1000
        return 2

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

    def stuck(max_seconds, *_):
        clock.value += 1000
        return 1

    def good(max_seconds, *_):
        queue.counts["completed"] += 1
        clock.value += 1000
        return 0

    session(tmp_path, queue, stuck, clock)
    session(tmp_path, queue, stuck, clock)
    assert session(tmp_path, queue, good, clock)["no_progress_sessions"] == 0


def test_daily_worker_seconds_cap(tmp_path):
    queue, clock, calls = Queue(), Clock(), []

    def worker(max_seconds, *_):
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
        result = session(tmp_path, Queue(), lambda s, *_: calls.append(s) or 0, Clock())
    assert "another dispatcher" in result["reason"] and calls == []


def test_remote_command_has_runtime_limit_and_container_cleanup():
    settings = {"image": "img:1", "remote_dir": "/srv/w"}
    command = worker_host.remote_worker_command(settings, "drain", "sts-worker-drain", 3600)
    assert "timeout -k 60 3600 docker run" in command
    assert command.count("docker rm -f sts-worker-drain") == 2
    assert command.endswith("exit $code; }")


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
    monkeypatch.setattr(worker_host, "power", lambda settings, action: events.append(action) or "x")
    monkeypatch.setattr(worker_host, "wait_for_ssh", lambda settings, timeout: True)
    settings = {"key_path": Path("k"), "poll_seconds": "1", "ready_timeout": 10, "private_ip": "i"}
    assert worker_host.run_remote(settings, "cmd", session_timeout=5) == 124
    assert events == ["start", "terminated"]


def test_session_never_stops_the_worker_itself(monkeypatch):
    events = []
    monkeypatch.setattr(worker_host, "power", lambda settings, action: events.append(action) or "x")
    monkeypatch.setattr(worker_host, "wait_for_ssh", lambda settings, timeout: False)
    settings = {"key_path": Path("k"), "poll_seconds": "1", "ready_timeout": 10, "private_ip": "i"}
    assert worker_host.run_remote(settings, "cmd", session_timeout=5) == 1
    assert events == ["start"]


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

    def worker(max_seconds, *_):
        queue.counts["claimed"] = 1
        reads["after"] = 1
        clock.value += 100
        return 0

    result = dispatch.run_session(tmp_path, counts, worker, CFG, clock=clock, sleep=lambda s, *_: None)
    assert result["progress"] == 1 and result["no_progress_sessions"] == 0


def test_does_not_wake_while_jobs_are_claimed_elsewhere(tmp_path):
    queue, calls = Queue(), []
    queue.counts["claimed"] = 1
    result = session(tmp_path, queue, lambda s, *_: calls.append(s) or 0, Clock())
    assert result["reason"] == "jobs are being processed elsewhere" and calls == []


def guard_env(tmp_path, free_after_restart, logs, running="true"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "free"
    state.write_text("2800")
    (tmp_path / "logs").write_text(logs)
    (tmp_path / "calls").write_text("")
    (bin_dir / "nvidia-smi").write_text(f"#!/bin/bash\ncat {state}\n")
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        f'echo "$1" >> {tmp_path / "calls"}\n'
        f'case "$1" in inspect) echo {running};; logs) cat {tmp_path / "logs"};; '
        f"restart) echo {free_after_restart} > {state};; esac\n"
    )
    (bin_dir / "sleep").write_text("#!/bin/bash\n")
    for name in ("nvidia-smi", "docker", "sleep"):
        (bin_dir / name).chmod(0o755)
    return bin_dir, state


def run_guard(tmp_path, bin_dir):
    script = worker_host.gpu_guard_script("demo-app", 6144, 15)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    result = subprocess.run(["bash", "-c", f"( {script} )"], capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, (tmp_path / "calls").read_text().split()


IDLE = '2026 INFO usage_log: usage {"event": "heartbeat", "requests": 0, "open_websockets": 0, "tiles_inferred": 0}\n' * 15
BUSY = IDLE + '2026 INFO usage_log: usage {"event": "heartbeat", "requests": 4, "open_websockets": 1, "tiles_inferred": 9}\n'
OPENED = IDLE + '2026 INFO usage_log: usage {"event": "ws_open", "at": "x"}\n'


def test_guard_does_nothing_when_enough_gpu_memory_is_free(tmp_path):
    bin_dir, state = guard_env(tmp_path, 14000, IDLE)
    state.write_text("9000")
    assert run_guard(tmp_path, bin_dir)[::2] == (0, [])


def test_guard_restarts_an_idle_demo_and_proceeds(tmp_path):
    bin_dir, _ = guard_env(tmp_path, 14000, IDLE)
    code, out, calls = run_guard(tmp_path, bin_dir)
    assert code == 0 and calls == ["inspect", "logs", "restart"] and "restarting" in out


@pytest.mark.parametrize("logs", [BUSY, OPENED, IDLE[: len(IDLE) // 3], ""])
def test_guard_never_restarts_a_demo_that_is_not_provably_idle(tmp_path, logs):
    bin_dir, _ = guard_env(tmp_path, 14000, logs)
    code, out, calls = run_guard(tmp_path, bin_dir)
    assert code == worker_host.GPU_BUSY_EXIT and "restart" not in calls and "deferring" in out


def test_guard_defers_when_the_demo_is_not_the_holder(tmp_path):
    bin_dir, _ = guard_env(tmp_path, 14000, IDLE, running="false")
    code, out, calls = run_guard(tmp_path, bin_dir)
    assert code == worker_host.GPU_BUSY_EXIT and "restart" not in calls


def test_guard_defers_when_restart_does_not_free_enough(tmp_path):
    bin_dir, _ = guard_env(tmp_path, 3000, IDLE)
    code, out, calls = run_guard(tmp_path, bin_dir)
    assert code == worker_host.GPU_BUSY_EXIT and calls[-1] == "restart" and "still only" in out


def test_remote_command_runs_the_guard_before_the_worker_and_stops_when_it_defers():
    settings = {"image": "img:1", "remote_dir": "/srv/w", "gpu_free_container": "demo-app", "gpu_min_free_mib": 6144, "gpu_idle_minutes": 15}
    command = worker_host.remote_worker_command(settings, "drain", "sts-worker-drain", 3600)
    assert command.index("gpu_guard") < command.index("docker image inspect") < command.index("timeout -k 60")
    assert command.index("&& { docker rm -f") > command.index("gpu_guard")
    plain = worker_host.remote_worker_command({"image": "img:1", "remote_dir": "/srv/w"}, "drain", "sts-worker-drain", 3600)
    assert "gpu_guard" not in plain


def test_deferred_session_is_recorded_but_never_opens_the_breaker(tmp_path):
    clock = Clock()
    for _ in range(5):
        result = session(tmp_path, Queue(), lambda s, *_: worker_host.GPU_BUSY_EXIT, clock)
        assert result["action"] == "deferred" and result["no_progress_sessions"] == 0
        clock.value += 1000
    assert dispatch.load_state(tmp_path)["no_progress_sessions"] == 0
    assert len(dispatch.load_state(tmp_path)["sessions"]) == 5


def test_a_deferral_backs_the_dispatcher_off_so_a_busy_gpu_is_not_woken_every_two_minutes(tmp_path):
    clock, calls = Clock(), []
    first = session(tmp_path, Queue(), lambda s, *_: calls.append(s) or worker_host.GPU_BUSY_EXIT, clock)
    assert first["action"] == "deferred"
    clock.value += 120
    blocked = session(tmp_path, Queue(), lambda s, *_: calls.append(s) or 0, clock)
    assert blocked["action"] == "skip" and "backing off" in blocked["reason"] and len(calls) == 1
    clock.value += 900
    assert session(tmp_path, Queue(), lambda s, *_: calls.append(s) or 0, clock)["action"] == "ran"
