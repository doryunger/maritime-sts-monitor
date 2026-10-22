import argparse
import ipaddress
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
MANAGER_IMAGE = "maritime-sts-manager"


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def manager_command(env_path, *args):
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        "1000:1000",
        "--env-file",
        str(env_path),
        "-e",
        "STS_CONFIG_PATH=/app/config.yaml",
        "-v",
        f"{ROOT / 'data'}:/app/data",
        MANAGER_IMAGE,
        "-m",
        "maritime_tip_cue.catalogue",
        *args,
    ]
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def describe_instance(client, private_ip):
    response = client.describe_instances(
        Filters=[{"Name": "private-ip-address", "Values": [private_ip]}]
    )
    instances = [
        instance
        for reservation in response["Reservations"]
        for instance in reservation["Instances"]
    ]
    if len(instances) != 1:
        raise RuntimeError("Expected exactly one EC2 worker for the configured private IP")
    return instances[0]


def wait_for_state(client, private_ip, target, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        instance = describe_instance(client, private_ip)
        state = instance["State"]["Name"]
        print(f"EC2={state}", flush=True)
        if state == target:
            return instance
        if state in ("terminated", "shutting-down"):
            raise RuntimeError(f"EC2 entered an unusable state: {state}")
        time.sleep(10)
    raise TimeoutError(f"EC2 did not reach {target} within {timeout_seconds} seconds")


def start_instance(client, private_ip, timeout_seconds):
    instance = describe_instance(client, private_ip)
    state = instance["State"]["Name"]
    if state == "stopping":
        instance = wait_for_state(client, private_ip, "stopped", timeout_seconds)
        state = instance["State"]["Name"]
    if state == "stopped":
        client.start_instances(InstanceIds=[instance["InstanceId"]])
        print("EC2=start-requested", flush=True)
        instance = wait_for_state(client, private_ip, "running", timeout_seconds)
    elif state == "pending":
        instance = wait_for_state(client, private_ip, "running", timeout_seconds)
    elif state != "running":
        raise RuntimeError(f"EC2 cannot be started from state: {state}")
    return instance


def wait_for_ssh(client, private_ip, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = describe_instance(client, private_ip)["State"]["Name"]
        if state != "running":
            raise RuntimeError(f"EC2 left running state before SSH was ready: {state}")
        try:
            with socket.create_connection((private_ip, 22), timeout=4):
                return
        except OSError:
            print("Waiting for EC2 private SSH", flush=True)
            time.sleep(10)
    raise TimeoutError("EC2 private SSH did not become reachable")


def run_worker(private_ip, key_path, remote_dir, image, job_id):
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("OpenSSH client was not found on PATH")
    worker_command = (
        f"cd {shlex.quote(remote_dir)} && test -f config.local.yaml && "
        "test -d /opt/ai2-models && nvidia-smi >/dev/null && "
        "df -Pk / | awk 'NR==2 { exit !($4 >= 3145728) }' && "
        "docker image inspect "
        f"{shlex.quote(image)} >/dev/null && "
        f"docker run --rm --gpus all --shm-size=15g "
        "--network host --env-file .env "
        '-v "$PWD/config.local.yaml:/app/config.local.yaml:ro" '
        '-v "$PWD/data:/app/data" -v /opt/ai2-models:/opt/ai2-models:ro '
        f"{shlex.quote(image)} run --job-id {shlex.quote(job_id)}"
    )
    return subprocess.run(
        [
            ssh,
            "-tt",
            "-i",
            str(key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ServerAliveInterval=20",
            "-o",
            "ServerAliveCountMax=120",
            f"ubuntu@{private_ip}",
            worker_command,
        ],
        check=False,
    ).returncode


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
    values = read_env(env_path)
    private_ip = os.environ.get("WORKER_PRIVATE_IP", values.get("WORKER_PRIVATE_IP", ""))
    region = os.environ.get("AWS_REGION", values.get("AWS_REGION", ""))
    key_path = Path(
        os.environ.get("WORKER_KEY_PATH", values.get("WORKER_KEY_PATH", "/home/ubuntu/.ssh/geogen-key.pem"))
    ).expanduser()
    remote_dir = os.environ.get(
        "WORKER_REMOTE_DIR", values.get("WORKER_REMOTE_DIR", "/home/ubuntu/maritime-sts-monitor")
    )
    image = os.environ.get(
        "WORKER_IMAGE", values.get("WORKER_IMAGE", "maritime-sts-gpu-worker:test")
    )
    result_timeout_seconds = int(os.environ.get("JOB_RESULT_TIMEOUT_SECONDS", "1800"))
    timeout_seconds = int(os.environ.get("EC2_READY_TIMEOUT_SECONDS", "1200"))
    if not private_ip or not region:
        raise RuntimeError("WORKER_PRIVATE_IP and AWS_REGION must be configured")
    try:
        ipaddress.ip_address(private_ip)
    except ValueError as error:
        raise RuntimeError("WORKER_PRIVATE_IP must be an IP address") from error
    if not key_path.is_file():
        raise RuntimeError(f"EC2 key was not found at {key_path}")
    if stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        raise RuntimeError(f"EC2 key permissions must be 600: {key_path}")
    if not shutil.which("docker"):
        raise RuntimeError("Docker was not found on PATH")
    manager_args = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        "1000:1000",
        "--env-file",
        str(env_path),
        "-e",
        "STS_CONFIG_PATH=/app/config.yaml",
        "-v",
        f"{ROOT / 'data'}:/app/data",
        MANAGER_IMAGE,
    ]
    if args.job_id:
        command = manager_args + [
            "-m",
            "maritime_tip_cue.job_manager",
            "once",
            "--job-id",
            args.job_id,
        ]
    else:
        command = manager_args + [
            "-m",
            "maritime_tip_cue.job_manager",
            "scan-one",
        ]
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
    session = boto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or values.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or values.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN") or values.get("AWS_SESSION_TOKEN"),
        region_name=region,
    )
    client = session.client("ec2")
    start_instance(client, private_ip, timeout_seconds)
    wait_for_ssh(client, private_ip, timeout_seconds)
    print(f"SSH worker command active for job {job_id}", flush=True)
    worker_exit = run_worker(private_ip, key_path, remote_dir, image, job_id)
    result = wait_for_result(env_path, job_id, result_timeout_seconds)
    print(json.dumps({"job": result, "worker_exit_code": worker_exit}, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
