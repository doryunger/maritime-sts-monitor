import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from maritime_tip_cue.config import ROOT

TTL_SECONDS = 20
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}


def control_host() -> dict:
    return json.loads((ROOT / "data" / "secrets" / "control_host.json").read_text(encoding="utf-8-sig"))


def fetch() -> dict:
    with _lock:
        if _cache["value"] is not None and time.monotonic() - _cache["at"] < TTL_SECONDS:
            return _cache["value"]
        control = control_host()
        identity = Path(os.path.expandvars(os.path.expanduser(control["identity_file"])))
        if not identity.is_absolute():
            identity = ROOT / identity
        ssh = shutil.which("ssh.exe") or shutil.which("ssh")
        if not ssh:
            raise RuntimeError("OpenSSH is required to read the ledger")
        project = control.get("project_dir", "/home/ubuntu/maritime-sts-monitor")
        remote = (
            f"cd {project} && sudo docker run --rm --network host --user 1000:1000 --env-file .env "
            '-e STS_CONFIG_PATH=/app/config.yaml -v "$PWD/data:/app/data" maritime-sts-manager '
            "-m maritime_tip_cue.catalogue ledger"
        )
        result = subprocess.run(
            [ssh, "-i", str(identity), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             f"{control['user']}@{control['host']}", remote],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-300:] or f"ssh exited {result.returncode}")
        _cache.update(at=time.monotonic(), value=json.loads(result.stdout))
        return _cache["value"]
