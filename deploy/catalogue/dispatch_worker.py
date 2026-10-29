import argparse
import fcntl
import json
import os
import sys
import time

import worker_host
from worker_host import ROOT

CONTAINER = "sts-worker-drain"
MANAGER_MODULE = "maritime_tip_cue.job_manager"


def limits():
    return {
        "max_session_seconds": int(os.environ.get("DISPATCH_MAX_SESSION_SECONDS", str(4 * 3600))),
        "max_no_progress_sessions": int(os.environ.get("DISPATCH_MAX_NO_PROGRESS_SESSIONS", "3")),
        "max_seconds_per_day": int(os.environ.get("DISPATCH_MAX_SECONDS_PER_DAY", str(12 * 3600))),
        "settle_seconds": int(os.environ.get("DISPATCH_SETTLE_SECONDS", "180")),
    }


def state_dir():
    return ROOT / "data" / "dispatcher"


def load_state(directory):
    path = directory / "state.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"no_progress_sessions": 0, "sessions": []}


def save_state(directory, state):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "state.json.tmp"
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(directory / "state.json")


def seconds_last_day(state, now):
    total = 0
    for session in state["sessions"]:
        end = session["started"] + session["seconds"]
        if end > now - 86400:
            total += end - max(session["started"], now - 86400)
    return total


def decide(counts, state, now, cfg, paused):
    if paused:
        return False, "paused"
    if state["no_progress_sessions"] >= cfg["max_no_progress_sessions"]:
        return False, "no-progress breaker open; inspect failures then run with --reset"
    if counts["ready"] == 0:
        return False, "no ready jobs"
    if counts["claimed"] > 0:
        return False, "jobs are being processed elsewhere"
    if seconds_last_day(state, now) >= cfg["max_seconds_per_day"]:
        return False, "daily worker-seconds cap reached"
    return True, "ready jobs"


def settle(queue_counts, cfg, sleep=time.sleep):
    waited = 0
    counts = queue_counts()
    while counts["claimed"] > 0 and waited < cfg["settle_seconds"]:
        sleep(10)
        waited += 10
        counts = queue_counts()
    return counts


def run_session(
    directory, queue_counts, run_worker, cfg, clock=time.time, paused=False, dry_run=False, sleep=time.sleep
):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "dispatcher.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"action": "skip", "reason": "another dispatcher session is active"}
        state = load_state(directory)
        before = queue_counts()
        now = clock()
        go, reason = decide(before, state, now, cfg, paused or (directory / "paused").exists())
        if not go or dry_run:
            return {"action": "would-run" if go else "skip", "reason": reason, "queue": before}
        started = clock()
        exit_code = run_worker(cfg["max_session_seconds"])
        seconds = max(0, round(clock() - started))
        after = settle(queue_counts, cfg, sleep)
        progress = after["completed"] - before["completed"]
        deferred = exit_code == worker_host.GPU_BUSY_EXIT
        if progress > 0:
            state["no_progress_sessions"] = 0
        elif not deferred:
            state["no_progress_sessions"] += 1
        state["sessions"] = [s for s in state["sessions"] if s["started"] + s["seconds"] > clock() - 86400]
        state["sessions"].append({"started": started, "seconds": seconds, "exit": exit_code, "progress": progress})
        save_state(directory, state)
        return {
            "action": "deferred" if deferred else "ran",
            "exit": exit_code,
            "seconds": seconds,
            "progress": progress,
            "no_progress_sessions": state["no_progress_sessions"],
            "queue": after,
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    directory = state_dir()
    if args.pause or args.resume:
        directory.mkdir(parents=True, exist_ok=True)
        flag = directory / "paused"
        if args.pause:
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
        return 0
    if args.reset:
        state = load_state(directory)
        state["no_progress_sessions"] = 0
        save_state(directory, state)
        return 0
    if os.geteuid() != 0:
        raise RuntimeError("Run this script with sudo because the Ubuntu account has no Docker socket access")
    env_path = ROOT / ".env"
    settings = worker_host.load_settings(env_path)

    def queue_counts():
        return worker_host.manager_json(env_path, MANAGER_MODULE, "queue-status")

    def run_worker(max_seconds):
        command = worker_host.remote_worker_command(settings, "drain", CONTAINER, max_seconds)
        return worker_host.run_remote(settings, command, session_timeout=max_seconds + settings["ready_timeout"])

    if args.preflight:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".probe"
        probe.write_text("ok")
        probe.unlink()
        report = {
            "settings": "ok",
            "queue": queue_counts(),
            "state_dir_writable": True,
            "paused": (directory / "paused").exists(),
            "breaker": load_state(directory)["no_progress_sessions"],
            "limits": limits(),
        }
        print(json.dumps(report, sort_keys=True))
        return 0
    result = run_session(directory, queue_counts, run_worker, limits(), dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["action"] != "ran" or result["exit"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
