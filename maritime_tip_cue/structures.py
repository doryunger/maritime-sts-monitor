import json
import sys
import time

import requests

from maritime_tip_cue import hotspots
from maritime_tip_cue.config import ROOT, settings
from maritime_tip_cue.sts import rules as rules_mod


def path():
    return ROOT / settings()["structures"]["path"]


def query(bbox: list[float], filters: list[str]) -> str:
    w, s, e, n = bbox
    area = f"({s},{w},{n},{e})"
    return "[out:json][timeout:120];(" + "".join(f + area + ";" for f in filters) + ");out center tags;"


def fetch() -> dict:
    cfg = settings()["structures"]
    filters = rules_mod.load()["fixed_structures"]["osm_filters"]
    features, seen = [], set()
    for site in hotspots.load():
        for attempt in range(4):
            r = requests.post(cfg["overpass_url"], data={"data": query(site["bbox"], filters)}, headers={"User-Agent": "maritime-sts-monitor"}, timeout=180)
            if r.ok and r.text.lstrip().startswith("{"):
                break
            time.sleep(20 * (attempt + 1))
        else:
            raise RuntimeError(f"Overpass failed for {site['name']}: {r.status_code}")
        for el in r.json()["elements"]:
            key = (el["type"], el["id"])
            lat = el.get("lat", el.get("center", {}).get("lat"))
            lon = el.get("lon", el.get("center", {}).get("lon"))
            if key in seen or lat is None:
                continue
            seen.add(key)
            tags = el.get("tags", {})
            kind = tags.get("seamark:type") or tags.get("man_made")
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                             "properties": {"osm": f"{el['type']}/{el['id']}", "kind": kind, "name": tags.get("name") or tags.get("seamark:name"), "site": site["name"]}})
    doc = {"type": "FeatureCollection", "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "features": features}
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc


def load() -> list[tuple[float, float, str]]:
    if not path().exists():
        return []
    doc = json.loads(path().read_text(encoding="utf-8"))
    return [(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1], f["properties"]["kind"]) for f in doc["features"]]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    doc = fetch() if len(sys.argv) > 1 and sys.argv[1] == "fetch" else {"features": [{"properties": {"site": "", "kind": k}} for _, _, k in load()]}
    by = {}
    for f in doc["features"]:
        key = (f["properties"]["site"], f["properties"]["kind"])
        by[key] = by.get(key, 0) + 1
    print(f"{len(doc['features'])} structures in {path()}: {by}")
