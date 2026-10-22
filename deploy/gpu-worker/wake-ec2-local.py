import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import boto3


def read_env(path):
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


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
        raise RuntimeError(
            f"Expected one EC2 instance with private IP {private_ip}, found {len(instances)}"
        )
    return instances[0]


def wait_for_state(client, private_ip, target_state, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        instance = describe_instance(client, private_ip)
        state = instance["State"]["Name"]
        print(f"EC2={state}", flush=True)
        if state == target_state:
            return instance
        if state in {"terminated", "shutting-down"}:
            raise RuntimeError(f"EC2 entered an unusable state: {state}")
        time.sleep(15)
    raise TimeoutError(f"EC2 did not reach {target_state} within {timeout_seconds} seconds")


def wait_for_public_ssh(client, private_ip, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        instance = describe_instance(client, private_ip)
        public_ip = instance.get("PublicIpAddress")
        state = instance["State"]["Name"]
        if state in {"terminated", "shutting-down"}:
            raise RuntimeError(f"EC2 entered an unusable state: {state}")
        if public_ip:
            try:
                with socket.create_connection((public_ip, 22), timeout=4):
                    return public_ip
            except OSError:
                pass
        print(f"EC2={state} waiting for public SSH", flush=True)
        time.sleep(10)
    raise TimeoutError("Public SSH did not become reachable before the timeout")


def main():
    repo_root = Path(__file__).resolve().parents[2]
    env_values = read_env(repo_root / ".env")
    default_key = Path.home() / "Downloads" / "geogen-key.pem"
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--worker-private-ip",
        default=os.environ.get("WORKER_PRIVATE_IP", env_values.get("WORKER_PRIVATE_IP")),
        required=not (os.environ.get("WORKER_PRIVATE_IP") or env_values.get("WORKER_PRIVATE_IP")),
    )
    parser.add_argument(
        "--key-path",
        type=Path,
        default=Path(os.environ.get("WORKER_KEY_PATH", str(default_key))).expanduser(),
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", env_values.get("AWS_REGION")))
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()

    if not args.region:
        parser.error("AWS_REGION is missing from the environment and repo .env")
    if not args.key_path.is_file():
        parser.error(f"EC2 key was not found at {args.key_path}")
    ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not ssh:
        parser.error("OpenSSH client was not found on PATH")

    session = boto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", env_values.get("AWS_ACCESS_KEY_ID")),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", env_values.get("AWS_SECRET_ACCESS_KEY")),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN", env_values.get("AWS_SESSION_TOKEN")),
        region_name=args.region,
    )
    client = session.client("ec2")
    instance = describe_instance(client, args.worker_private_ip)
    state = instance["State"]["Name"]
    print(f"EC2={state} instance={instance['InstanceId']}", flush=True)

    if state == "stopping":
        wait_for_state(client, args.worker_private_ip, "stopped", args.timeout_seconds)
        state = "stopped"
    if state == "stopped":
        client.start_instances(InstanceIds=[instance["InstanceId"]])
        print("EC2=start-requested", flush=True)
        wait_for_state(client, args.worker_private_ip, "running", args.timeout_seconds)
    elif state == "pending":
        wait_for_state(client, args.worker_private_ip, "running", args.timeout_seconds)
    elif state != "running":
        raise RuntimeError(f"EC2 cannot be woken from state: {state}")

    public_ip = wait_for_public_ssh(client, args.worker_private_ip, args.timeout_seconds)
    print(f"SSH target ubuntu@{public_ip}", flush=True)
    return subprocess.run(
        [
            ssh,
            "-tt",
            "-i",
            str(args.key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"ubuntu@{public_ip}",
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
