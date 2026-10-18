import math

import numpy as np
from affine import Affine
from rasterio.warp import transform as warp_transform
from scipy.ndimage import binary_fill_holes, binary_opening, distance_transform_edt, label

from maritime_tip_cue.sts.rules import s1_config


def chip_window(vh: np.ndarray, vv: np.ndarray, transform, crs) -> dict:
    return {
        "vh": vh,
        "vv": vv,
        "transform": Affine(*[float(v) for v in transform]),
        "crs": str(crs),
        "shape": vh.shape,
    }


def to_xy(lon: float, lat: float, crs: str) -> tuple[float, float]:
    x, y = warp_transform("EPSG:4326", crs, [lon], [lat])
    return x[0], y[0]


def analyse(w: dict, x: float, y: float, c: dict) -> dict:
    tr = w["transform"]
    pixel = c["pixel_m"]
    hw = c["chip_half_m"]
    n = int(2 * hw / pixel)
    c0, r0 = int((x - hw - tr.c) / pixel), int((tr.f - y - hw) / pixel)
    chips = {}
    for pol in ("vv", "vh"):
        db = np.where(w[pol] > 1e-9, 10 * np.log10(np.clip(w[pol], 1e-9, None)), np.nan)
        chip = np.full((n, n), np.nan)
        rs, re = max(r0, 0), min(r0 + n, db.shape[0])
        cs, ce = max(c0, 0), min(c0 + n, db.shape[1])
        chip[rs - r0 : re - r0, cs - c0 : ce - c0] = db[rs:re, cs:ce]
        chips[pol] = chip
    cc, rr = np.meshgrid(np.arange(n), np.arange(n))
    px = tr.c + (c0 + cc + 0.5) * pixel
    py = tr.f - (r0 + rr + 0.5) * pixel
    dist = np.hypot(px - x, py - y)
    valid = np.isfinite(chips["vv"]) & np.isfinite(chips["vh"])
    return {"vv": chips["vv"], "vh": chips["vh"], "dist": dist, "valid": valid, "px": px, "py": py}


def rc(a: dict, x: float, y: float, pixel: float) -> tuple[int, int]:
    return int(round((a["py"][0, 0] - y) / pixel)), int(round((x - a["px"][0, 0]) / pixel))


def grow(a: dict, x: float, y: float, thr: dict, c: dict) -> np.ndarray | None:
    ok = np.ones(a["valid"].shape, bool)
    score = np.full(a["valid"].shape, np.inf)
    for pol, t in thr.items():
        ok &= a[pol] >= t
        score = np.minimum(score, a[pol] - t)
    r, cc = rc(a, x, y, c["pixel_m"])
    if not ok[r, cc]:
        cand = np.where(a["dist"] <= c["start_search_m"], score, -np.inf)
        cand = np.where(np.isfinite(cand), cand, -np.inf)
        best = cand.max()
        if best < 0:
            return None
        ys, xs = np.where(cand == best)
        k = np.argmin(a["dist"][ys, xs])
        r, cc = int(ys[k]), int(xs[k])
    lab, _ = label(ok, np.ones((3, 3)))
    return lab == lab[r, cc]


def clean(m: np.ndarray, a: dict, x: float, y: float, c: dict) -> np.ndarray | None:
    pad = np.pad(m, 1)
    t_px = 2 * float(distance_transform_edt(pad).max())
    if c["mode"] == "fixed":
        r = max(1, int(c["ray_max_width_px"]) // 2)
    else:
        r = max(1, int(round(c["thickness_fraction"] * t_px / 2)))
    k = np.arange(-r, r + 1)
    disk = (k[:, None] ** 2 + k[None, :] ** 2) <= r * r
    op = binary_opening(pad, disk)[1:-1, 1:-1]
    if not op.any():
        return None
    lab, _ = label(op, np.ones((3, 3)))
    rr, cc = rc(a, x, y, c["pixel_m"])
    if lab[rr, cc] > 0:
        keep = lab == lab[rr, cc]
    else:
        ys, xs = np.where(op)
        j = np.argmin((ys - rr) ** 2 + (xs - cc) ** 2)
        keep = lab == lab[ys[j], xs[j]]
    return binary_fill_holes(keep)


def axis(xy: np.ndarray) -> np.ndarray | None:
    if len(xy) < 2:
        return None
    cov = np.cov((xy - xy.mean(0)).T)
    if not np.all(np.isfinite(cov)):
        return None
    w, v = np.linalg.eigh(cov)
    if w[1] < 1e-9:
        return None
    return v[:, 1]


def min_rect(xy: np.ndarray, step: float, pixel: float) -> tuple[float, float, float]:
    best = None
    for t in np.arange(0, 180, step):
        u = np.array([math.cos(math.radians(t)), math.sin(math.radians(t))])
        n = np.array([-u[1], u[0]])
        a, b = np.ptp(xy @ u) + pixel, np.ptp(xy @ n) + pixel
        if best is None or a * b < best[0] - 1e-9:
            best = (a * b, t, a, b)
    _, t, a, b = best
    if b > a:
        a, b, t = b, a, (t + 90) % 180
    return t, a, b


def describe(mc: np.ndarray, a: dict, pol: str, c: dict) -> dict:
    rr, cc = np.where(mc)
    xy = np.column_stack([a["px"][rr, cc], a["py"][rr, cc]])
    t, length, width = min_rect(xy, c["rect_angle_step_deg"], c["pixel_m"])
    v = a[pol][rr, cc]
    bm = v >= np.quantile(v, c["bright_quantile"])
    return {"rect": (t, length, width), "xy": xy, "rr": rr, "cc": cc, "bm": bm}


def parallel(d: dict, a: dict, pol: str, peak: float, c: dict) -> None:
    xy, bm = d["xy"], d["bm"]
    t, _, width = d["rect"]
    if bm.sum() < c["min_bright_px"]:
        return
    u = np.array([math.cos(math.radians(t)), math.sin(math.radians(t))])
    n = np.array([-u[1], u[0]])
    xb = xy[bm]
    v = a[pol][d["rr"], d["cc"]][bm]
    w = 10 ** (np.maximum(v - (peak - c["outline_drop_db"]), 0.0) / 10)
    q = xb @ n
    qc = 0.5 * (float((xy @ n).min()) + float((xy @ n).max()))
    rng = np.random.default_rng(c["par_seed"])
    starts = [(qc - f * width, qc + f * width) for f in c["par_start_fractions"]]
    for _ in range(c["par_random_starts"]):
        starts.append(tuple(rng.uniform(q.min(), q.max(), 2)))
    best = None
    for o in starts:
        o = np.array(o, float)
        lab = None
        for _ in range(c["par_max_iter"]):
            nl = np.argmin(np.abs(q[:, None] - o[None]), 1)
            if lab is not None and np.array_equal(nl, lab):
                break
            lab = nl
            for k in range(2):
                if (lab == k).any():
                    o[k] = np.average(q[lab == k], weights=w[lab == k])
        if min((lab == 0).sum(), (lab == 1).sum()) < c["par_min_line_px"]:
            continue
        cost = float((w * (q - o[lab]) ** 2).sum())
        if best is None or cost < best[0] - 1e-9:
            best = (cost, o.copy(), lab.copy())
    if best is None:
        return
    _, o, lab = best
    order = np.argsort(o)
    o, remap = o[order], np.argsort(order)
    lab = remap[lab]
    ext = []
    for k in range(2):
        pr = xb[lab == k] @ u
        ext.append((u * pr.min() + n * o[k], u * pr.max() + n * o[k]))
    d["par"] = {"ext": ext}


def build(window: dict, start: tuple[float, float], detections: list[dict], rules: dict) -> dict:
    c = s1_config(rules)
    pol = c["polarisation"]
    own = [d for d in detections if d.get("own")]
    st = min(own, key=lambda d: math.hypot(d["x"] - start[0], d["y"] - start[1]))
    a = analyse(window, st["x"], st["y"], c)
    near = (a["dist"] <= c["peak_radius_m"]) & a["valid"]
    peak = float(np.nanpercentile(a[pol][near], c["peak_percentile"]))
    obj = {"a": a, "pol": pol, "d": None, "straight": False}
    m = grow(a, st["x"], st["y"], {pol: peak - c["outline_drop_db"]}, c)
    if m is None:
        return obj
    mc = clean(m, a, st["x"], st["y"], c)
    if mc is None:
        return obj
    d = describe(mc, a, pol, c)
    parallel(d, a, pol, peak, c)
    obj["d"] = d
    obj["straight"] = axis(d["xy"]) is not None
    return obj
