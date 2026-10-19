import json

from maritime_tip_cue.sts import ai2point
from maritime_tip_cue.watch.geo import Shore


def plain(value):
    return json.loads(json.dumps(value, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def prepare(features: list[dict], source: str, site: dict, shore: Shore, rules: dict) -> tuple[list[dict], dict]:
    feats = ai2point.correct_features(features, source, rules)
    w, s, e, n = site["bbox"]
    counts = {"raw": len(feats), "outside_site": 0, "near_shore": 0}
    dets = []
    for f in feats:
        p = f["properties"]
        a = p.get("attributes") or {}
        lon, lat = p["longitude"], p["latitude"]
        if not (w <= lon <= e and s <= lat <= n):
            counts["outside_site"] += 1
            continue
        if rules["detection_clean"]["drop_on_land"] and shore.distance_m(lon, lat) < site["min_shore_distance_m"]:
            counts["near_shore"] += 1
            continue
        dets.append(
            {
                "det_id": f"{p['col']}_{p['row']}",
                "lat": lat,
                "lon": lon,
                "length_m": a.get("length"),
                "width_m": a.get("width"),
                "heading": a.get("heading"),
                "speed_kn": a.get("speed"),
                "score": p["score"],
            }
        )
    counts["kept"] = len(dets)
    return dets, counts
