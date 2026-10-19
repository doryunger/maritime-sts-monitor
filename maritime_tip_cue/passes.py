import argparse
import math
from datetime import datetime, timedelta, timezone

import requests
from shapely.geometry import box, shape

from maritime_tip_cue import hotspots, land
from maritime_tip_cue.sts import rules as rules_mod

SOURCES = ("s1", "s2")


def search(url: str, body: dict) -> list[dict]:
    items, href, method, payload = [], f"{url}/search", "POST", body
    while href:
        r = requests.request(method, href, json=payload if method == "POST" else None, timeout=180)
        r.raise_for_status()
        page = r.json()
        items += page.get("features", [])
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not nxt or not page.get("features"):
            break
        method = nxt.get("method", "GET").upper()
        payload = (payload | nxt["body"]) if method == "POST" and nxt.get("merge") else nxt.get("body", payload)
        href = nxt["href"]
    return items


def area_km2(geom) -> float:
    return 0.0 if geom.is_empty else geom.area * 111.32**2 * math.cos(math.radians(geom.centroid.y))


def scene_id(source: str, item: dict) -> str | None:
    if source == "s2":
        return (item["properties"].get("s2:product_uri") or "").replace(".SAFE", "") or None
    return item["id"]


def datatake(sid: str, source: str) -> str:
    return sid.rsplit("_", 1)[0] if source == "s2" else sid


def find(start: datetime, end: datetime, sites: list[dict], rules: dict | None = None) -> list[dict]:
    cfg = (rules or rules_mod.load())["scene"]
    found: dict[str, dict] = {}
    latest: dict[str, str] = {}
    when = f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
    for site in sites:
        whole = box(*site["bbox"])
        for source in SOURCES:
            url, collection = cfg["catalogs"][source]
            for item in search(url, {"collections": [collection], "bbox": site["bbox"], "datetime": when, "limit": 200}):
                sid = scene_id(source, item)
                cloud = item["properties"].get("eo:cloud_cover")
                if not sid or (source == "s2" and cloud is not None and cloud > cfg["s2_max_cloud_pct"]):
                    continue
                geom = shape(item["geometry"])
                overlap = area_km2(geom.intersection(whole))
                if overlap < cfg["min_overlap_km2"]:
                    continue
                take = f"{site['name']}|{datatake(sid, source)}"
                if latest.get(take, "") > sid:
                    continue
                if take in latest:
                    found.pop(f"{site['name']}|{latest[take]}", None)
                latest[take] = sid
                found[f"{site['name']}|{sid}"] = {
                    "site": site["name"],
                    "source": source,
                    "scene": sid,
                    "time": item["properties"]["datetime"],
                    "cloud": cloud,
                    "overlap_km2": round(overlap),
                    "geometry": geom,
                    "item": item,
                }
    return sorted(found.values(), key=lambda f: (f["site"], f["time"]))


def plot(passes: list[dict], sites: list[dict], path: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    groups = [[s for s in sites if s["bbox"][0] > 90], [s for s in sites if s["bbox"][0] <= 90]]
    groups = [g for g in groups if g]
    fig, axes = plt.subplots(1, len(groups), figsize=(8 * len(groups), 7))
    axes = axes if len(groups) > 1 else [axes]
    colours = {"s1": "tab:blue", "s2": "tab:orange"}
    for ax, group in zip(axes, groups):
        names = {s["name"] for s in group}
        for p in passes:
            if p["site"] in names:
                xs, ys = p["geometry"].exterior.xy if p["geometry"].geom_type == "Polygon" else max(p["geometry"].geoms, key=lambda g: g.area).exterior.xy
                ax.plot(xs, ys, color=colours[p["source"]], lw=0.8, alpha=0.6)
        for s in group:
            w, so, e, n = s["bbox"]
            ax.plot([w, e, e, w, w], [so, so, n, n, so], color="red", lw=2)
            ax.text(w, n, s["name"], color="red", fontsize=9, va="bottom")
        xs = [v for s in group for v in (s["bbox"][0], s["bbox"][2])]
        ys = [v for s in group for v in (s["bbox"][1], s["bbox"][3])]
        ax.set_xlim(min(xs) - 2, max(xs) + 2)
        ax.set_ylim(min(ys) - 2, max(ys) + 2)
        ax.set_aspect(1 / math.cos(math.radians(sum(ys) / len(ys))))
        ax.set_title("hot spots (red), S1 (blue), S2 (orange)")
        for g in land.within((min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2)):
            for poly in getattr(g, "geoms", [g]):
                if poly.geom_type == "Polygon":
                    ax.fill(*poly.exterior.xy, color="0.85", zorder=0)
    fig.savefig(path, dpi=110, bbox_inches="tight")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.passes")
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--end", help="ISO end time, default now")
    parser.add_argument("--map", help="PNG path for the footprint map")
    args = parser.parse_args()
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else datetime.now(timezone.utc)
    sites = hotspots.load()
    passes = find(end - timedelta(days=args.days), end, sites)
    for p in passes:
        cloud = "" if p["cloud"] is None else f"cloud {p['cloud']:.0f}%"
        print(f"{p['site']:<15} {p['time'][:16]} {p['source']}  {p['overlap_km2']:>6} km2  {cloud:<10} {p['scene']}")
    for s in sites:
        n = {src: sum(1 for p in passes if p["site"] == s["name"] and p["source"] == src) for src in SOURCES}
        print(f"{s['name']:<15} S1 {n['s1']}  S2 {n['s2']}")
    if args.map:
        plot(passes, sites, args.map)
