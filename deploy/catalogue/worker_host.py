import ipaddress
import json
import os
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
MANAGER_IMAGE = "maritime-sts-manager"
GPU_BUSY_EXIT = 75


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def load_settings(env_path):
    values = read_env(env_path)

    def get(name, default=""):
        return os.environ.get(name, values.get(name, default))

    settings = {
        "env_path": env_path,
        "private_ip": get("WORKER_PRIVATE_IP"),
        "status_url": get("WORKER_WAKE_STATUS_URL"),
        "key_path": Path(get("WORKER_KEY_PATH", "/home/ubuntu/.ssh/geogen-key.pem")).expanduser(),
        "remote_dir": get("WORKER_REMOTE_DIR"),
        "image": get("WORKER_IMAGE", "maritime-sts-gpu-worker:test"),
        "ready_timeout": int(get("EC2_READY_TIMEOUT_SECONDS", "1200")),
        "poll_seconds": get("WAKE_POLL_SECONDS", "20"),
        "gpu_free_container": get("WORKER_GPU_FREE_CONTAINER"),
        "gpu_min_free_mib": int(get("WORKER_GPU_MIN_FREE_MIB", "5376")),
        "gpu_idle_minutes": int(get("WORKER_GPU_IDLE_MINUTES", "15")),
    }
    if not settings["private_ip"] or not settings["status_url"] or not settings["remote_dir"]:
        raise RuntimeError("WORKER_PRIVATE_IP, WORKER_WAKE_STATUS_URL and WORKER_REMOTE_DIR must be configured")
    try:
        ipaddress.ip_address(settings["private_ip"])
    except ValueError as error:
        raise RuntimeError("WORKER_PRIVATE_IP must be an IP address") from error
    parsed = urlparse(settings["status_url"])
    if parsed.scheme not in ("http", "https") or parsed.path.rstrip("/") != "/_wake/status":
        raise RuntimeError("WORKER_WAKE_STATUS_URL must point to the /_wake/status endpoint")
    key_path = settings["key_path"]
    if not key_path.is_file():
        raise RuntimeError(f"EC2 key was not found at {key_path}")
    if stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        raise RuntimeError(f"EC2 key permissions must be 600: {key_path}")
    if not shutil.which("docker"):
        raise RuntimeError("Docker was not found on PATH")
    if not (ROOT / "deploy" / "gpu-worker" / "wake-ec2-ubuntu.sh").is_file():
        raise RuntimeError("The EC2 wake script was not found")
    return settings


def manager_docker_command(env_path, module, *args):
    return [
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
        module,
        *args,
    ]


def manager_json(env_path, module, *args):
    command = manager_docker_command(env_path, module, *args)
    last_error = None
    for _ in range(30):
        result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_error = result.stderr.strip()
        if "holds the ledger lock" not in last_error:
            raise RuntimeError(last_error)
        time.sleep(1)
    raise RuntimeError(f"Manager ledger lock stayed held: {last_error}")


GPU_GUARD = (
    "free() { nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1; }; "
    'need=__NEED__; c=__CONTAINER__; idle=__IDLE__; '
    'if [ "$(free)" -ge "$need" ]; then exit 0; fi; '
    'echo "gpu_guard: only $(free) MiB free, need $need"; '
    "if ! docker inspect -f '{{.State.Running}}' \"$c\" 2>/dev/null | grep -q true; then "
    'echo "gpu_guard: $c is not running, something else holds the GPU"; exit __BUSY__; fi; '
    'logs=$(docker logs --since ${idle}m "$c" 2>&1); '
    "beats=$(printf '%s\\n' \"$logs\" | grep -c '\"event\": \"heartbeat\"'); "
    "busy=$(printf '%s\\n' \"$logs\" | grep '\"event\": \"heartbeat\"' | grep -vc '\"requests\": 0, \"open_websockets\": 0'); "
    "opened=$(printf '%s\\n' \"$logs\" | grep -c '\"event\": \"ws_open\"'); "
    'if [ "$beats" -lt $((idle - 5)) ] || [ "$busy" -gt 0 ] || [ "$opened" -gt 0 ]; then '
    'echo "gpu_guard: $c not provably idle for ${idle} min (heartbeats=$beats busy=$busy ws_open=$opened), deferring"; '
    "exit __BUSY__; fi; "
    'echo "gpu_guard: $c idle for ${idle} min, restarting it to free GPU memory"; '
    'docker restart "$c" >/dev/null || exit __BUSY__; sleep 15; '
    'if [ "$(free)" -lt "$need" ]; then echo "gpu_guard: still only $(free) MiB free after restart"; exit __BUSY__; fi; '
    'echo "gpu_guard: $(free) MiB free after restart"'
)


def gpu_guard_script(container, min_free_mib, idle_minutes):
    return (
        GPU_GUARD.replace("__NEED__", str(int(min_free_mib)))
        .replace("__CONTAINER__", shlex.quote(container))
        .replace("__IDLE__", str(int(idle_minutes)))
        .replace("__BUSY__", str(GPU_BUSY_EXIT))
    )


def remote_worker_command(settings, worker_args, container_name=None, max_seconds=None):
    image = settings["image"]
    s3_preflight = (
        "import boto3; from maritime_tip_cue.config import env,settings; "
        "boto3.client('s3',region_name=env('AWS_REGION')).list_objects_v2("
        "Bucket=env('S3_BUCKET_NAME'),Prefix=settings()['catalogue']['results_prefix'],MaxKeys=1)"
    )
    preflight = (
        "docker run --rm --network host --env-file .env "
        '-v "$PWD/config.local.yaml:/app/config.local.yaml:ro" '
        '-v "$PWD/data:/app/data" '
        f"--entrypoint python {shlex.quote(image)} -c {shlex.quote(s3_preflight)}"
    )
    guard = ""
    if settings.get("gpu_free_container"):
        script = gpu_guard_script(settings["gpu_free_container"], settings["gpu_min_free_mib"], settings["gpu_idle_minutes"])
        guard = f"( {script} ) && "
    name = f"--name {shlex.quote(container_name)} " if container_name else ""
    run = (
        f"docker run --rm {name}--gpus all --shm-size=15g "
        "--network host --env-file .env "
        '-v "$PWD/config.local.yaml:/app/config.local.yaml:ro" '
        '-v "$PWD/data:/app/data" '
        f"{shlex.quote(image)} {worker_args}"
    )
    head = (
        f"cd {shlex.quote(settings['remote_dir'])} && test -f config.local.yaml && "
        "test -d /opt/ai2-models && nvidia-smi >/dev/null && "
        f"{guard}"
        "df -Pk / | awk 'NR==2 { exit !($4 >= 3145728) }' && "
        f"docker image inspect {shlex.quote(image)} >/dev/null && {preflight} && "
    )
    if not (container_name and max_seconds):
        return head + run
    quoted = shlex.quote(container_name)
    cleanup = f"docker rm -f {quoted} >/dev/null 2>&1"
    return f"{head}{{ {cleanup}; timeout -k 60 {int(max_seconds)} {run}; code=$?; {cleanup}; exit $code; }}"


def run_remote(settings, remote_command, wake_timeout=None, session_timeout=None):
    wake_script = ROOT / "deploy" / "gpu-worker" / "wake-ec2-ubuntu.sh"
    bash = shutil.which("bash")
    if not bash:
        raise RuntimeError("Bash was not found on PATH")
    env = os.environ.copy()
    env["WORKER_KEY_PATH"] = str(settings["key_path"])
    env["POLL_SECONDS"] = settings["poll_seconds"]
    env["WAKE_TIMEOUT_SECONDS"] = str(wake_timeout or settings["ready_timeout"])
    process = subprocess.Popen(
        [bash, str(wake_script), settings["status_url"], settings["private_ip"], "--", remote_command],
        cwd=ROOT,
        env=env,
    )
    try:
        return process.wait(timeout=session_timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 124
