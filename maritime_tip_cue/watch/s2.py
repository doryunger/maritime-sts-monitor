from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
import requests
from affine import Affine
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import transform as warp_transform
from rasterio.windows import Window, from_bounds

from maritime_tip_cue.sts import s2pair, s2window
from maritime_tip_cue.watch import detector
from maritime_tip_cue.watch.geo import haversine_m, offset_m

SETS = {"B02_B03_B04_B08": 1, "B05_B06_B07_B8A_B11_B12": 2, "B01_B09_B10": 4}
REF = "B02_B03_B04_B08"
VERDICT_RANK = {"pair": 2, "possible_pair": 1}


def window_layers(scratch: Path) -> list[Path]:
    found = list((scratch / "windows" / "detector_predict").glob("*/layers/sentinel2"))
    return sorted(found, key=lambda p: int(p.parent.parent.name))


def locate(layers: list[Path], region: tuple) -> Path | None:
    west, south, east, north = region
    best = None
    for path in layers:
        with rasterio.open(path / REF / "geotiff.tif") as ref:
            xs, ys = warp_transform("EPSG:4326", ref.crs, [west, east, (west + east) / 2], [south, north, (south + north) / 2])
            b = ref.bounds
            inside = [b.left <= x <= b.right and b.bottom <= y <= b.top for x, y in zip(xs, ys)]
        if all(inside[:2]):
            return path
        if inside[2] and best is None:
            best = path
    return best


def read_bands(dsets: dict, r0: int, c0: int, h: int, w: int) -> dict[str, np.ndarray]:
    out = {}
    for name, factor in SETS.items():
        rr, cc = r0 // factor, c0 // factor
        hh, ww = (r0 + h - 1) // factor - rr + 1, (c0 + w - 1) // factor - cc + 1
        a = dsets[name].read(window=Window(cc, rr, ww, hh), boundless=True, fill_value=0).astype(np.float32)
        for band, img in zip(name.split("_"), a):
            if factor > 1:
                img = np.kron(img, np.ones((factor, factor), dtype=np.float32))
                img = img[r0 - rr * factor : r0 - rr * factor + h, c0 - cc * factor : c0 - cc * factor + w]
            out[band] = img
    return out


def load_window(layers: Path, region: tuple) -> dict:
    dsets = {name: rasterio.open(layers / name / "geotiff.tif") for name in SETS}
    try:
        ref = dsets[REF]
        west, south, east, north = region
        xs, ys = warp_transform("EPSG:4326", ref.crs, [west, east, west, east], [south, south, north, north])
        rows, cols = zip(*(ref.index(x, y) for x, y in zip(xs, ys)))
        r0, c0 = max(min(rows), 0), max(min(cols), 0)
        h, w = min(max(rows), ref.height) - r0, min(max(cols), ref.width) - c0
        bands = read_bands(dsets, r0, c0, h, w)
        return {"bands": bands, "transform": ref.transform * Affine.translation(c0, r0), "crs": str(ref.crs), "shape": (h, w), "scl": None}
    finally:
        for d in dsets.values():
            d.close()


def scl_dataset(scene_id: str, when: datetime, rules: dict):
    cfg = rules["s2_cloud_mask"]
    day = when.strftime("%Y-%m-%d")
    body = {
        "collections": [cfg["collection"]],
        "datetime": f"{day}T00:00:00Z/{day}T23:59:59Z",
        "limit": 50,
        "query": {"s2:mgrs_tile": {"eq": scene_id.split("_")[5][1:]}},
    }
    r = requests.post(f"{cfg['stac_url']}/search", json=body, timeout=120)
    r.raise_for_status()
    items = [f for f in r.json().get("features", []) if f["id"][:3] == scene_id[:3]]
    if not items:
        return None
    return rasterio.open(detector.sign(items[0]["assets"][cfg["asset"]]["href"]))


def scl_window(ds, bounds, height: int, width: int) -> np.ndarray:
    win = from_bounds(*bounds, transform=ds.transform)
    return ds.read(1, window=win, out_shape=(height, width), resampling=Resampling.nearest, boundless=True, fill_value=0).astype(np.uint8)


def candidates(dets: list[dict], rules: dict) -> list[dict]:
    floor = rules["pair_split"]["min_length_m"]
    return [d for d in dets if d["length_m"] is not None and d["length_m"] >= floor and d["width_m"] is not None and d["heading"] is not None]


def cluster_windows(cands: list[dict], cfg: dict) -> list[dict]:
    windows, left = [], list(cands)
    while left:
        seed = left[0]
        near = [h for h in left if haversine_m(h["lat"], h["lon"], seed["lat"], seed["lon"]) <= cfg["cluster_m"]]
        windows.append(
            {
                "lat": float(np.mean([h["lat"] for h in near])),
                "lon": float(np.mean([h["lon"] for h in near])),
                "members": [h["det_id"] for h in near],
            }
        )
        left = [h for h in left if h not in near]
    return windows


def extract(scratch: Path, cands: list[dict], scene: dict, rules: dict, folder: Path, log) -> list[dict]:
    cfg = rules["s2_window"]
    layers = window_layers(scratch)
    folder.mkdir(parents=True, exist_ok=True)
    index = []
    mask = {"ds": None, "tried": False}
    try:
        for i, w in enumerate(cluster_windows(cands, cfg)):
            region = (w["lon"] - cfg["region_half_lon_deg"], w["lat"] - cfg["region_half_lat_deg"], w["lon"] + cfg["region_half_lon_deg"], w["lat"] + cfg["region_half_lat_deg"])
            path = locate(layers, region) if layers else None
            entry = w | {"file": None, "region": region}
            if path is not None:
                win = load_window(path, region)
                if not mask["tried"]:
                    mask["tried"] = True
                    try:
                        mask["ds"] = scl_dataset(scene["scene"], datetime.fromisoformat(scene["time"].replace("Z", "+00:00")), rules)
                    except Exception as e:
                        log(f"{scene['scene']}: no cloud mask: {str(e)[:200]}")
                if mask["ds"] is not None and str(mask["ds"].crs) == win["crs"]:
                    win["scl"] = scl_window(mask["ds"], array_bounds(win["shape"][0], win["shape"][1], win["transform"]), *win["shape"])
                arrays = {f"band_{k}": v for k, v in win["bands"].items()}
                if win["scl"] is not None:
                    arrays["scl"] = win["scl"]
                name = f"w{i:03d}.npz"
                np.savez_compressed(folder / name, transform=np.array(win["transform"])[:6], crs=win["crs"], **arrays)
                entry["file"] = name
            index.append(entry)
    finally:
        if mask["ds"] is not None:
            mask["ds"].close()
    return index


def stored_window(folder: Path, name: str) -> dict:
    z = np.load(folder / name)
    bands = {k[5:]: z[k] for k in z.files if k.startswith("band_")}
    return s2window.window_from_arrays(bands, z["transform"], z["crs"], z["scl"] if "scl" in z.files else None)


def lonlat(win: dict, x: float, y: float) -> tuple[float, float]:
    lons, lats = warp_transform(win["crs"], "EPSG:4326", [x], [y])
    return lons[0], lats[0]


def hull_centre(win: dict, fix: dict, hull: dict | None) -> tuple[float | None, float | None]:
    if not hull:
        return None, None
    al, ac = s2window.axes(fix["heading"])
    lon, lat = lonlat(win, fix["x"] + hull["s_m"] * al[0] + hull["t_m"] * ac[0], fix["y"] + hull["s_m"] * al[1] + hull["t_m"] * ac[1])
    return lat, lon


def summarise(win: dict, res: dict) -> dict:
    fix = res.get("fix") or {}
    out = {
        "verdict": res["verdict"],
        "unmeasured_reason": res.get("unmeasured_reason"),
        "fix_heading": fix.get("heading"),
        "fix_length_m": fix.get("length_m"),
        "fix_source": fix.get("source"),
        "width_ratio": res.get("width_ratio"),
        "ratio_ab": res.get("ratio_ab"),
        "width_m_measured": res.get("width_m"),
        "b_offset_m": res.get("b_offset_m"),
        "cloud_beside_share": res.get("cloud_beside_share"),
        "reaches_water": res.get("reaches_water"),
        "a_fired": res.get("a_fired"),
        "b_fired": res.get("b_fired"),
        "weak_evidence": res.get("weak_evidence"),
        "shape_call": res.get("shape_call"),
    }
    for k, v in (res.get("shape") or {}).items():
        out[f"shape_{k}"] = v
    shape = res.get("shape") or {}
    hulls = {"a": res.get("hull_a"), "b": res.get("hull_b")}
    out["hull_b_source"] = "blob" if hulls["b"] else None
    if hulls["b"] is None and res.get("shape_call") == "pair" and "b_t_m" in shape:
        hulls["b"] = {"s_m": shape["b_s_m"], "t_m": shape["b_t_m"], "width_m": shape["b_width_m"], "length_m": shape["b_length_m"]}
        out["hull_b_source"] = "shape"
    for which in ("a", "b"):
        h = hulls[which]
        lat, lon = hull_centre(win, fix, h) if "x" in fix else (None, None)
        out |= {
            f"hull_{which}_lat": lat,
            f"hull_{which}_lon": lon,
            f"hull_{which}_width_m": h["width_m"] if h else None,
            f"hull_{which}_length_m": h["length_m"] if h else None,
        }
    out["plateau_sigma"] = res["hull_b"]["plateau_sigma"] if res.get("hull_b") else None
    return out


def rectangle(check: dict, which: str):
    lat, lon = check.get(f"hull_{which}_lat"), check.get(f"hull_{which}_lon")
    length, width = check.get(f"hull_{which}_length_m"), check.get(f"hull_{which}_width_m")
    if None in (lat, lon, length, width, check.get("fix_heading")):
        return None
    return lat, lon, check["fix_heading"], length, width


def inside(d: dict, rect, margin: float) -> bool:
    if rect is None:
        return False
    lat, lon, heading, length, width = rect
    _, along, across = offset_m({"lat": lat, "lon": lon}, d, heading)
    return along is not None and along <= length / 2 + margin and across <= width / 2 + margin


def group(dets: list[dict], checks: dict[str, dict], margin: float) -> dict[str, dict]:
    by_id = {d["det_id"]: d for d in dets}
    seeds = sorted(
        (d for d in dets if checks.get(d["det_id"], {}).get("verdict") in VERDICT_RANK),
        key=lambda d: (-VERDICT_RANK[checks[d["det_id"]]["verdict"]], -d["score"], -d["length_m"], d["det_id"]),
    )
    claimed: dict[str, dict] = {}
    for n, s in enumerate(seeds, 1):
        sid = s["det_id"]
        if sid in claimed:
            continue
        chk = checks[sid]
        ra, rb = rectangle(chk, "a"), rectangle(chk, "b")
        members = {sid: "anchor"}
        for d in dets:
            k = d["det_id"]
            if k == sid or k in claimed:
                continue
            in_a, in_b = inside(d, ra, margin), inside(d, rb, margin)
            if in_a and in_b:
                in_a = offset_m({"lat": ra[0], "lon": ra[1]}, d, None)[0] <= offset_m({"lat": rb[0], "lon": rb[1]}, d, None)[0]
                in_b = not in_a
            if in_a:
                members[k] = "duplicate"
            elif in_b:
                members[k] = "hull_b_box"
        second = [k for k, r in members.items() if r == "hull_b_box"]
        if second and rb is not None:
            nearest = min(second, key=lambda k: offset_m({"lat": rb[0], "lon": rb[1]}, by_id[k], None)[0])
            for k in second:
                if k != nearest and chk["verdict"] == "pair":
                    members[k] = "duplicate"
        for k, role in members.items():
            claimed[k] = {"record": f"{n:03d}", "role": role}
    return claimed


def analyse(folder: Path, dets: list[dict], index: list[dict], rules: dict) -> list[dict]:
    shape_bands = rules["s2_window"]["shape_bands"]
    by_id = {d["det_id"]: d for d in dets}
    checks: dict[str, dict] = {}
    for w in index:
        win = stored_window(folder, w["file"]) if w["file"] else None
        for k in sorted(w["members"]):
            d = by_id[k]
            if win is None:
                checks[k] = {"verdict": "unmeasured", "unmeasured_reason": "no detector pixels cover the hull"}
                continue
            hull = {key: d[key] for key in ("lat", "lon", "length_m", "width_m", "heading", "speed_kn")}
            res = s2pair.run(win, hull, rules, shape_bands)
            checks[k] = summarise(win, res)
    claimed = group(dets, checks, rules["s2_window"]["inside_margin_m"])
    rows = []
    for d in dets:
        if d["det_id"] not in checks and d["det_id"] not in claimed:
            continue
        chk = checks.get(d["det_id"])
        c = claimed.get(d["det_id"])
        rows.append(d | {"role": c["role"] if c else (chk["verdict"] if chk else None), "record": c["record"] if c else None, "check": chk, "verdict": chk["verdict"] if chk else None})
    return rows
