from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform
from scipy.ndimage import binary_opening, gaussian_filter, label

from maritime_tip_cue.sts import s1object, s1pair


def load_windows(scratch: Path, windows: dict) -> dict:
    out = {}
    for w in windows["windows"]:
        if w["group"] != "detector_predict":
            continue
        base = scratch / "windows" / "detector_predict" / w["name"] / "layers" / "sentinel1"
        arrays = {}
        for pol in ("vv", "vh"):
            with rasterio.open(base / pol / "geotiff.tif") as ds:
                a = ds.read(1).astype(np.float32)
                np.square(a, out=a)
                arrays[pol] = a
                tr, crs, shape = ds.transform, ds.crs, ds.shape
        out[int(w["name"])] = arrays | {"transform": tr, "crs": str(crs), "shape": shape}
    return out


def place(dets: list[dict], wins: dict, pixel: float) -> tuple[list[dict], int]:
    kept, outside = [], 0
    for d in dets:
        for i, w in wins.items():
            x, y = s1object.to_xy(d["lon"], d["lat"], w["crs"])
            c, r = (x - w["transform"].c) / pixel, (w["transform"].f - y) / pixel
            if 0 <= c < w["shape"][1] and 0 <= r < w["shape"][0]:
                kept.append(d | {"region_idx": i, "x": x, "y": y})
                break
        else:
            outside += 1
    return kept, outside


def sea_level(db: np.ndarray, ring: np.ndarray) -> tuple[float, float]:
    x = db[ring]
    for _ in range(3):
        m = np.median(x)
        s = 1.4826 * np.median(np.abs(x - m))
        x = x[x <= m + 4 * s]
    return float(m), float(s)


def local_object(win: dict, x: float, y: float, cfg: dict, pixel: float) -> dict | None:
    tr = win["transform"]
    n = int(cfg["chip_half_m"] / pixel)
    c = int((x - tr.c) / pixel)
    r = int((tr.f - y) / pixel)
    r0, r1, c0, c1 = max(r - n, 0), min(r + n, win["shape"][0]), max(c - n, 0), min(c + n, win["shape"][1])
    if r1 - r0 < 10 or c1 - c0 < 10:
        return None
    sm = 10 * np.log10(np.clip(gaussian_filter(win["vv"][r0:r1, c0:c1], cfg["smooth_px"]), 1e-9, None))
    rr, cc = np.mgrid[r0:r1, c0:c1]
    d = np.hypot((cc + 0.5) * pixel + tr.c - x, tr.f - (rr + 0.5) * pixel - y)
    ring = (d >= cfg["sea_annulus_inner_m"]) & (win["vv"][r0:r1, c0:c1] > 0)
    if ring.sum() < 200:
        return None
    m, s = sea_level(sm, ring)
    pre = sm > m + cfg["obj_k_sigma"] * s
    mask = binary_opening(pre, np.ones((cfg["open_px"], cfg["open_px"])))
    lab, k = label(mask, np.ones((3, 3)))
    if k == 0:
        return None
    best, pick = 1e9, None
    for j in range(1, k + 1):
        dj = float(d[lab == j].min())
        if dj < best:
            best, pick = dj, j
    if best > cfg["assoc_max_m"]:
        return None
    sel = lab == pick
    return {"rows": rr[sel], "cols": cc[sel]}


def pca(xs: np.ndarray, ys: np.ndarray, pixel: float) -> dict:
    cx, cy = xs.mean(), ys.mean()
    vecs = np.linalg.eigh(np.cov(np.vstack([xs - cx, ys - cy])))[1]
    v, u = vecs[:, 1], vecs[:, 0]
    pl = (xs - cx) * v[0] + (ys - cy) * v[1]
    pw = (xs - cx) * u[0] + (ys - cy) * u[1]
    return {"x": cx, "y": cy, "length": float(pl.max() - pl.min()) + pixel, "width": float(pw.max() - pw.min()) + pixel}


def group_objects(wins: dict, dets: list[dict], cfg: dict, pixel: float) -> list[dict]:
    parent: dict[int, int] = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    owner: dict[tuple[int, int, int], int] = {}
    comps: dict[int, dict] = {}
    for i, d in enumerate(dets):
        parent[i] = i
        w = d["region_idx"]
        comp = local_object(wins[w], d["x"], d["y"], cfg, pixel)
        if comp is None:
            continue
        comps[i] = comp
        for r, c in zip(comp["rows"], comp["cols"]):
            k = (w, int(r), int(c))
            if k in owner:
                a, b = find(owner[k]), find(i)
                if a != b:
                    parent[a] = b
            else:
                owner[k] = i
    groups: dict[int, list[int]] = {}
    for i in comps:
        groups.setdefault(find(i), []).append(i)
    objs = []
    for g in groups.values():
        w = dets[g[0]]["region_idx"]
        tr = wins[w]["transform"]
        pix = {(int(r), int(c)) for i in g for r, c in zip(comps[i]["rows"], comps[i]["cols"])}
        arr = np.array(sorted(pix))
        m = pca(tr.c + (arr[:, 1] + 0.5) * pixel, tr.f - (arr[:, 0] + 0.5) * pixel, pixel)
        lon, lat = warp_transform(wins[w]["crs"], "EPSG:4326", [m["x"]], [m["y"]])
        objs.append(
            {
                "obj_id": f"w{w}_{int(m['x'] // pixel)}_{int(m['y'] // pixel)}",
                "win": w,
                "det_ids": [dets[i]["det_id"] for i in g],
                "x": m["x"],
                "y": m["y"],
                "lat": lat[0],
                "lon": lon[0],
                "length_m": m["length"],
                "width_m": m["width"],
                "ai2_max_length_m": max((dets[i]["length_m"] or 0) for i in g),
            }
        )
    return objs


def save_chips(folder: Path, objs: list[dict], dets: list[dict], wins: dict, rules: dict) -> list[dict]:
    folder.mkdir(parents=True, exist_ok=True)
    pixel = rules["s1_outline"]["pixel_m"]
    n = int(rules["s1_outline"]["chip_half_m"] / pixel)
    pad = rules["s1_objects"]["chip_neighbour_m"]
    out = []
    for o in objs:
        w = wins[o["win"]]
        tr = w["transform"]
        c = int((o["x"] - tr.c) / pixel)
        r = int((tr.f - o["y"]) / pixel)
        r0, r1, c0, c1 = max(r - n, 0), min(r + n, w["shape"][0]), max(c - n, 0), min(c + n, w["shape"][1])
        x0, y0 = tr.c + c0 * pixel, tr.f - r0 * pixel
        mine = set(o["det_ids"])
        near = [
            {"x": d["x"], "y": d["y"], "length_m": d["length_m"], "width_m": d["width_m"], "heading": d["heading"], "score": d["score"], "det_id": d["det_id"], "own": d["det_id"] in mine}
            for d in dets
            if d["region_idx"] == o["win"] and x0 - pad <= d["x"] <= x0 + (c1 - c0) * pixel + pad and y0 + pad >= d["y"] >= y0 - (r1 - r0) * pixel - pad
        ]
        name = f"{o['obj_id']}.npz"
        np.savez_compressed(folder / name, vh=w["vh"][r0:r1, c0:c1], vv=w["vv"][r0:r1, c0:c1], transform=np.array([pixel, 0.0, x0, 0.0, -pixel, y0]), crs=w["crs"])
        out.append(o | {"file": name, "detections": near})
    return out


def build_object(folder: Path, obj: dict, rules: dict) -> dict:
    z = np.load(folder / obj["file"])
    window = s1object.chip_window(z["vh"], z["vv"], z["transform"], z["crs"])
    return s1object.build(window, (obj["x"], obj["y"]), obj["detections"], rules)


def evaluate_pass(items: list[tuple[Path, dict]], rules: dict) -> list[dict]:
    built = [build_object(folder, o, rules) for folder, o in items]
    return s1pair.evaluate(built, rules)


def pass_of(scene_id: str) -> str:
    parts = scene_id.split("_")
    return "_".join([parts[0], parts[6], parts[7]])
