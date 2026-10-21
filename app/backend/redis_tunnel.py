import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from redis import Redis

from maritime_tip_cue.config import ROOT, env


def redis_client():
    return Redis.from_url(env("STS_REDIS_URL"), decode_responses=True, socket_connect_timeout=2, socket_timeout=3)


def ensure_tunnel():
    client = redis_client()
    try:
        client.ping()
        return
    except Exception:
        pass
    url = urlsplit(env("STS_REDIS_URL"))
    local_port = url.port or 6379
    try:
        with socket.create_connection(("127.0.0.1", local_port), timeout=1):
            raise RuntimeError("The local Redis tunnel port is occupied but Redis did not respond")
    except OSError:
        pass
    secret_path = ROOT / "data" / "secrets" / "control_host.json"
    control = json.loads(secret_path.read_text(encoding="utf-8-sig"))
    identity = Path(control["identity_file"])
    if not identity.is_absolute():
        identity = ROOT / identity
    ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not ssh:
        raise RuntimeError("OpenSSH is required to connect to the Redis host")
    destination = f"{env('STS_REDIS_TUNNEL_TARGET')}:{env('STS_REDIS_TUNNEL_PORT')}"
    forward = f"127.0.0.1:{local_port}:{destination}"
    command = [
        ssh,
        "-i",
        str(identity),
        "-N",
        "-L",
        forward,
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        f"{control['user']}@{control['host']}",
    ]
    flags = 0x00000200 if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    for _ in range(20):
        if process.poll() is not None:
            raise RuntimeError("SSH tunnel process exited before Redis became reachable")
        try:
            client.ping()
            return
        except Exception:
            time.sleep(0.5)
    process.terminate()
    raise RuntimeError("Redis did not respond through the SSH tunnel")


def main():
    try:
        ensure_tunnel()
    except Exception as error:
        print(f"Redis tunnel setup failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
