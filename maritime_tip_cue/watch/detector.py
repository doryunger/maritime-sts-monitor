import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from shapely.geometry import shape

from maritime_tip_cue.config import settings

RUNNER = Path(__file__).with_name("ai2_runner.py")
MPC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"


def ai2_root() -> Path:
    return Path(settings()["ai2"]["root"])


def python() -> Path:
    venv = ai2_root() / ".venv"
    win = venv / "Scripts" / "python.exe"
    unix = venv / "bin" / "python"
    if win.exists():
        return win
    return unix if unix.exists() else Path(sys.executable)


def sign(href: str) -> str:
    r = requests.get(MPC_SIGN, params={"href": href}, timeout=60)
    r.raise_for_status()
    return r.json()["href"]


def fetch(url: str, path: Path) -> Path:
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(path)
    return path


def s1_reference(item: dict, rules: dict) -> dict:
    cfg = rules["s1_reference_image"]
    t0 = datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00"))
    lo, hi, pref = cfg["min_days_before"], cfg["max_days_before"], cfg["preferred_days_before"]
    body = {
        "collections": [rules["scene"]["catalogs"]["s1"][1]],
        "intersects": item["geometry"],
        "datetime": f"{t0 - timedelta(days=hi):%Y-%m-%dT%H:%M:%SZ}/{t0 - timedelta(days=lo):%Y-%m-%dT%H:%M:%SZ}",
        "limit": 200,
    }
    r = requests.post(f"{rules['scene']['catalogs']['s1'][0]}/search", json=body, timeout=120)
    r.raise_for_status()
    target = shape(item["geometry"])
    props = item["properties"]
    cands = [
        c
        for c in r.json().get("features", [])
        if c["properties"].get("sat:orbit_state") == props.get("sat:orbit_state")
        and c["properties"].get("sar:instrument_mode") == cfg["instrument_mode"]
    ]
    if not cands:
        raise RuntimeError(f"no Sentinel-1 reference {lo}-{hi} days before {item['id']}")

    def score(c):
        t = datetime.fromisoformat(c["properties"]["datetime"].replace("Z", "+00:00"))
        return (target.intersection(shape(c["geometry"])).area / target.area, -abs((t0 - t).days - pref))

    return max(cands, key=score)


def s1_inputs(item: dict, rules: dict, work: Path, log) -> dict:
    ref = s1_reference(item, rules)
    log(f"s1 reference {ref['id']}")
    paths = {}
    for role, it in (("image", item), ("historical1", ref)):
        paths[role] = {}
        for pol in ("vv", "vh"):
            dst = work / f"{it['id']}_{pol}.tif"
            fetch(sign(it["assets"][pol]["href"]), dst)
            paths[role][pol] = str(dst.resolve())
    return paths


def detect(source: str, scene: dict, rules: dict, work: Path, region: tuple, log) -> tuple[float, dict, list[dict]]:
    cfg = rules["detectors"]
    work = work.resolve()
    scratch = work / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    crops = work / "crops"
    crops.mkdir(exist_ok=True)
    out_geojson = work / "out.geojson"
    env = os.environ | {k: str(v) for k, v in settings()["ai2"]["env"].items()} | {"PYTHONPATH": str(ai2_root() / "rslearn_projects")}
    task = {"geojson_path": str(out_geojson), "crop_path": str(crops) + os.sep}
    if source == "s1":
        task |= s1_inputs(scene["item"], rules, work, log)
        args = ["--tasks", json.dumps([task]), "--score_threshold", str(rules["scene"]["s1_min_score"])]
    else:
        task |= {"scene_id": scene["scene"]}
        args = ["--tasks", json.dumps([task])]
    windows_path = work / "windows.json"
    spec = work / "run_spec.json"
    spec.write_text(
        json.dumps({"project": cfg[f"{source}_pipeline"], "regions": [list(region)], "windows_path": str(windows_path), "args": args + ["--scratch_path", str(scratch) + os.sep]}),
        encoding="utf-8",
    )
    start = time.monotonic()
    with (work / "detector.log").open("w", encoding="utf-8") as f:
        proc = subprocess.run(
            [str(python()), str(RUNNER), str(spec)],
            cwd=ai2_root() / "rslearn_projects",
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=cfg[f"{source}_timeout_min"] * 60,
        )
    if proc.returncode != 0 or not out_geojson.exists():
        tail = (work / "detector.log").read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        raise RuntimeError(f"{cfg[f'{source}_pipeline']} exit {proc.returncode}: " + " | ".join(tail))
    if not windows_path.exists():
        raise RuntimeError("detector ran without reporting its windows")
    features = json.loads(out_geojson.read_text(encoding="utf-8"))["features"]
    return time.monotonic() - start, json.loads(windows_path.read_text(encoding="utf-8")), features
