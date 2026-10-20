import os
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env()


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (add it to .env)")
    return value


@cache
def settings() -> dict:
    path = Path(os.environ.get("STS_CONFIG_PATH") or ROOT / "config.yaml")
    if not path.is_absolute():
        path = ROOT / path
    return yaml.safe_load(path.read_text())
