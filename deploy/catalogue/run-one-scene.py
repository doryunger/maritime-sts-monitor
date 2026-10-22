import argparse
import json
import os
import shlex
import subprocess
import sys
import time

import worker_host
from worker_host import ROOT


def manager_command(env_path, *args):
    return worker_host.manager_json(env_path, "maritime_tip_cue.catalogue", *args)


def run_worker(settings, job_id, timeout_seconds):
    command = worker_host.remote_worker_command(settings, f"run --job-id {shlex.quote(job_id)}")
    return worker_host.run_remote(settings, command, wake_timeout=timeout_seconds)


def wait_for_result(env_path, job_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = manager_command(env_path, "job-status", "--job-id", job_id)
        status = result["status"]
        print(f"JOB={job_id} status={status}", flush=True)
        if status in ("succeeded", "failed", "retry_wait"):
            return result
        time.sleep(10)
    raise TimeoutError(f"Job {job_id} was not finalized within {timeout_seconds} seconds")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Run this script with sudo because the Ubuntu account has no Docker socket access")
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise RuntimeError(f"Manager environment file not found: {env_path}")
    settings = worker_host.load_settings(env_path)
    result_timeout_seconds = int(os.environ.get("JOB_RESULT_TIMEOUT_SECONDS", "1800"))
    timeout_seconds = settings["ready_timeout"]
    module = "maritime_tip_cue.job_manager"
    if args.job_id:
        command = worker_host.manager_docker_command(env_path, module, "once", "--job-id", args.job_id)
    else:
        command = worker_host.manager_docker_command(env_path, module, "scan-one")
    dispatch_process = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if dispatch_process.returncode:
        if dispatch_process.stdout:
            print(dispatch_process.stdout.rstrip(), file=sys.stderr)
        if dispatch_process.stderr:
            print(dispatch_process.stderr.rstrip(), file=sys.stderr)
        return dispatch_process.returncode
    dispatch_result = json.loads(dispatch_process.stdout)
    dispatched = dispatch_result.get("dispatched")
    if dispatched is None:
        if args.job_id:
            raise RuntimeError(f"Job {args.job_id} was not dispatched")
        scan = dispatch_result["scan"]
        print(
            json.dumps(
                {
                    "status": "no-new-scene",
                    "channels": [
                        {
                            "site": channel["site"],
                            "source": channel["source"],
                            "status": channel["status"],
                            "new": channel.get("new", 0),
                            "baseline": channel.get("baseline", False),
                        }
                        for channel in scan["channels"]
                    ],
                    "errors": scan["errors"],
                },
                sort_keys=True,
            )
        )
        return 0
    job_id = dispatched["job_id"]
    if dispatched["scenes"] != 1:
        raise RuntimeError(f"Selected job {job_id} contains {dispatched['scenes']} scenes")
    print(f"SSH worker command active for job {job_id}", flush=True)
    worker_exit = run_worker(settings, job_id, timeout_seconds)
    current = manager_command(env_path, "job-status", "--job-id", job_id)
    if worker_exit and current["status"] in ("pending", "dispatched"):
        print(json.dumps({"job": current, "worker_exit_code": worker_exit}, sort_keys=True))
        return 1
    result = wait_for_result(env_path, job_id, result_timeout_seconds)
    print(json.dumps({"job": result, "worker_exit_code": worker_exit}, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
