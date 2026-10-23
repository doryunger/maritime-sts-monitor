import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from maritime_tip_cue.config import ROOT

PORT = 8010


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    if not listening(PORT):
        control = json.loads((ROOT / "data" / "secrets" / "control_host.json").read_text(encoding="utf-8-sig"))
        identity = Path(os.path.expandvars(os.path.expanduser(control["identity_file"])))
        if not identity.is_absolute():
            identity = ROOT / identity
        ssh = shutil.which("ssh.exe") or shutil.which("ssh")
        if not ssh:
            print("OpenSSH is required", file=sys.stderr)
            return 1
        command = [
            ssh, "-i", str(identity), "-N", "-L", f"127.0.0.1:{PORT}:127.0.0.1:{PORT}",
            "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            f"{control['user']}@{control['host']}",
        ]
        flags = 0x00000200 | 0x08000000 if os.name == "nt" else 0
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(20):
            if listening(PORT):
                break
            time.sleep(0.5)
        else:
            print("The SSH tunnel did not come up", file=sys.stderr)
            return 1
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
