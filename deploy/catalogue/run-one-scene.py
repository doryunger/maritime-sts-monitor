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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANAGER_IMAGE = "maritime-sts-manager"
WAKE_POLL_SECONDS = 20


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


def wake_status(controller_url):
    request = urllib.request.Request(
        f"{controller_url}/_wake/status",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise RuntimeError("Wake controller status request failed") from None


def wait_for_worker(controller_url, private_ip):
    while True:
        status = wake_status(controller_url)
        state = status.get("ec2_state", "unknown")
        stage = status.get("stage", "unknown")
        print(f"EC2={state} stage={stage}", flush=True)
        if stage == "error":
            raise RuntimeError("Wake controller reported an error")
        if stage == "ready":
            try:
                with socket.create_connection((private_ip, 22), timeout=3):
                    return
            except OSError:
                print("Waiting for EC2 private SSH", flush=True)
        time.sleep(WAKE_POLL_SECONDS)


def run_worker(controller_url, private_ip, key_path, remote_dir, image, job_id):
    wait_for_worker(controller_url, private_ip)
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
    process = subprocess.Popen(
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
            "ServerAliveCountMax=30",
            f"ubuntu@{private_ip}",
            worker_command,
        ]
    )
    try:
        while process.poll() is None:
            time.sleep(WAKE_POLL_SECONDS)
            status = wake_status(controller_url)
            state = status.get("ec2_state", "unknown")
            stage = status.get("stage", "unknown")
            print(f"EC2={state} stage={stage}", flush=True)
            if stage == "error":
                raise RuntimeError("Wake controller reported an error")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    return process.returncode


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
    if os.geteuid() != 0:
        raise RuntimeError("Run this script with sudo because the Ubuntu account has no Docker socket access")
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise RuntimeError(f"Manager environment file not found: {env_path}")
    values = read_env(env_path)
    controller_url = os.environ.get("CONTROLLER_URL", values.get("CONTROLLER_URL", "")).rstrip("/")
    private_ip = os.environ.get("WORKER_PRIVATE_IP", values.get("WORKER_PRIVATE_IP", ""))
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
    if not controller_url or not private_ip:
        raise RuntimeError("CONTROLLER_URL and WORKER_PRIVATE_IP must be configured")
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
    scan_process = subprocess.run(
        [
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
            "maritime_tip_cue.job_manager",
            "scan-one",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if scan_process.returncode:
        if scan_process.stdout:
            print(scan_process.stdout.rstrip(), file=sys.stderr)
        if scan_process.stderr:
            print(scan_process.stderr.rstrip(), file=sys.stderr)
        return scan_process.returncode
    scan_result = json.loads(scan_process.stdout)
    dispatched = scan_result.get("dispatched")
    if dispatched is None:
        scan = scan_result["scan"]
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
    print(f"Wake-script flow active for job {job_id}", flush=True)
    worker_exit = run_worker(controller_url, private_ip, key_path, remote_dir, image, job_id)
    result = wait_for_result(env_path, job_id, result_timeout_seconds)
    print(json.dumps({"job": result, "worker_exit_code": worker_exit}, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
