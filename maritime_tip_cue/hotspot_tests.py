import json
from pathlib import Path

from maritime_tip_cue.config import ROOT, settings


def load_plan() -> dict:
    config = settings()["hotspot_tests"]
    path = Path(config["areas_path"])
    if not path.is_absolute():
        path = ROOT / path
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1 or not isinstance(plan.get("areas"), list):
        raise ValueError("Unsupported hotspot-test area plan")
    names = set()
    for area in plan["areas"]:
        name = area.get("name")
        bbox = area.get("bbox")
        if not isinstance(name, str) or not name.startswith("test_") or name in names:
            raise ValueError("Hotspot-test area names must be unique and start with test_")
        if area.get("pipeline") != "hotspot_test" or not isinstance(area.get("area_version"), int):
            raise ValueError(f"Invalid hotspot-test area metadata for {name}")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
            raise ValueError(f"Invalid bounding box for {name}")
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError(f"Invalid bounding box order for {name}")
        if not isinstance(area.get("min_shore_distance_m"), (int, float)):
            raise ValueError(f"Invalid shore distance for {name}")
        if not area.get("sources") or any(source not in ("s1", "s2") for source in area["sources"]):
            raise ValueError(f"Invalid sources for {name}")
        if not isinstance(area.get("lookback_days"), int) or area["lookback_days"] < 1:
            raise ValueError(f"Invalid lookback days for {name}")
        names.add(name)
    return plan


def load() -> list[dict]:
    return [
        {key: area[key] for key in ("name", "bbox", "min_shore_distance_m", "pipeline", "area_version", "label")}
        for area in load_plan()["areas"]
    ]


def sources_for(name: str) -> tuple[str, ...]:
    area = next((item for item in load_plan()["areas"] if item["name"] == name), None)
    if area is None:
        raise KeyError(name)
    return tuple(area["sources"])


def lookback_days(name: str) -> int:
    area = next((item for item in load_plan()["areas"] if item["name"] == name), None)
    if area is None:
        raise KeyError(name)
    return area["lookback_days"]


def findings_prefix() -> str:
    return settings()["hotspot_tests"]["findings_prefix"].rstrip("/") + "/"


def artifacts_prefix() -> str:
    return settings()["hotspot_tests"]["artifacts_prefix"].rstrip("/") + "/"


def reviews_prefix() -> str:
    return settings()["hotspot_tests"]["reviews_prefix"].rstrip("/") + "/"
