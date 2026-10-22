import argparse
import ipaddress
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

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


def run_worker(status_url, private_ip, key_path, remote_dir, image, job_id, timeout_seconds):
    wake_script = ROOT / "deploy" / "gpu-worker" / "wake-ec2-ubuntu.sh"
    bash = shutil.which("bash")
    if not bash:
        raise RuntimeError("Bash was not found on PATH")
    s3_preflight = (
        "import boto3; from maritime_tip_cue.config import env,settings; "
        "boto3.client('s3',region_name=env('AWS_REGION')).list_objects_v2("
        "Bucket=env('S3_BUCKET_NAME'),Prefix=settings()['catalogue']['results_prefix'],MaxKeys=1)"
    )
    preflight_command = (
        "docker run --rm --network host --env-file .env "
        '-v "$PWD/config.local.yaml:/app/config.local.yaml:ro" '
        '-v "$PWD/data:/app/data" '
        f"--entrypoint python {shlex.quote(image)} -c {shlex.quote(s3_preflight)}"
    )
    worker_command = (
        f"cd {shlex.quote(remote_dir)} && test -f config.local.yaml && "
        "test -d /opt/ai2-models && nvidia-smi >/dev/null && "
        "df -Pk / | awk 'NR==2 { exit !($4 >= 3145728) }' && "
        "docker image inspect "
        f"{shlex.quote(image)} >/dev/null && "
        f"{preflight_command} && "
        f"docker run --rm --gpus all --shm-size=15g "
        "--network host --env-file .env "
        '-v "$PWD/config.local.yaml:/app/config.local.yaml:ro" '
        '-v "$PWD/data:/app/data" -v /opt/ai2-models:/opt/ai2-models:ro '
        f"{shlex.quote(image)} run --job-id {shlex.quote(job_id)}"
    )
    wake_env = os.environ.copy()
    wake_env["WORKER_KEY_PATH"] = str(key_path)
    wake_env["POLL_SECONDS"] = os.environ.get("WAKE_POLL_SECONDS", "20")
    wake_env["WAKE_TIMEOUT_SECONDS"] = str(timeout_seconds)
    return subprocess.run(
        [
            bash,
            str(wake_script),
            status_url,
            private_ip,
            "--",
            worker_command,
        ],
        cwd=ROOT,
        env=wake_env,
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
    status_url = os.environ.get("WORKER_WAKE_STATUS_URL", values.get("WORKER_WAKE_STATUS_URL", ""))
    key_path = Path(
        os.environ.get("WORKER_KEY_PATH", values.get("WORKER_KEY_PATH", "/home/ubuntu/.ssh/geogen-key.pem"))
    ).expanduser()
    remote_dir = os.environ.get("WORKER_REMOTE_DIR", values.get("WORKER_REMOTE_DIR", ""))
    image = os.environ.get(
        "WORKER_IMAGE", values.get("WORKER_IMAGE", "maritime-sts-gpu-worker:test")
    )
    result_timeout_seconds = int(os.environ.get("JOB_RESULT_TIMEOUT_SECONDS", "1800"))
    timeout_seconds = int(os.environ.get("EC2_READY_TIMEOUT_SECONDS", "1200"))
    if not private_ip or not status_url or not remote_dir:
        raise RuntimeError("WORKER_PRIVATE_IP, WORKER_WAKE_STATUS_URL and WORKER_REMOTE_DIR must be configured")
    try:
        ipaddress.ip_address(private_ip)
    except ValueError as error:
        raise RuntimeError("WORKER_PRIVATE_IP must be an IP address") from error
    parsed_status_url = urlparse(status_url)
    if parsed_status_url.scheme not in ("http", "https") or parsed_status_url.path.rstrip("/") != "/_wake/status":
        raise RuntimeError("WORKER_WAKE_STATUS_URL must point to the /_wake/status endpoint")
    if not key_path.is_file():
        raise RuntimeError(f"EC2 key was not found at {key_path}")
    if stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        raise RuntimeError(f"EC2 key permissions must be 600: {key_path}")
    if not shutil.which("docker"):
        raise RuntimeError("Docker was not found on PATH")
    if not (ROOT / "deploy" / "gpu-worker" / "wake-ec2-ubuntu.sh").is_file():
        raise RuntimeError("The EC2 wake script was not found")
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
    print(f"SSH worker command active for job {job_id}", flush=True)
    worker_exit = run_worker(status_url, private_ip, key_path, remote_dir, image, job_id, timeout_seconds)
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
