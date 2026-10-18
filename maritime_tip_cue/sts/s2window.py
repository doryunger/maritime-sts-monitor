import math

import numpy as np
from affine import Affine
from rasterio.warp import transform as warp_transform
from scipy import ndimage as ndi
from scipy.ndimage import map_coordinates


def window_from_arrays(bands: dict, transform, crs, scl=None) -> dict:
    ref = next(iter(bands.values()))
    return {
        "bands": bands,
        "transform": Affine(*[float(v) for v in transform]),
        "crs": str(crs),
        "shape": ref.shape,
        "scl": scl,
    }


def block_threshold(ndwi: np.ndarray, valid: np.ndarray, cfg: dict) -> tuple[float, float] | None:
    vals = ndwi[valid]
    if vals.size < cfg["min_pixels"]:
        return None
    hist, edges = np.histogram(vals, bins=cfg["histogram_bins"], range=(-1, 1))
    mode = float((edges[hist.argmax()] + edges[hist.argmax() + 1]) / 2)
    near = vals[np.abs(vals - mode) <= cfg["mode_window"]]
    if near.size < cfg["min_water_share"] * vals.size:
        return None
    sigma = max(1.4826 * float(np.median(np.abs(near - np.median(near)))), cfg["min_sigma"])
    return mode, mode - max(cfg["k_sigma"] * sigma, cfg["min_drop"])


def axes(heading: float) -> tuple[np.ndarray, np.ndarray]:
    r = math.radians(heading)
    return np.array([math.sin(r), math.cos(r)]), np.array([math.cos(r), -math.sin(r)])


def pixel_stats(arrays: dict, win: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    w = win["water"]
    names = win["shape_bands"]
    v = np.stack([arrays[n] for n in names])
    wv = np.array([w["median"][n] for n in names])
    norm = np.linalg.norm(v, axis=0)
    cosine = np.tensordot(wv, v, axes=(0, 0)) / np.maximum(norm * np.linalg.norm(wv), 1e-9)
    angle = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    z = np.mean([(arrays[n] - w["median"][n]) / w["sigma"][n] for n in win["excess_bands"]], axis=0)
    return angle, norm / np.linalg.norm(wv), z


def water_levels(win: dict, water: np.ndarray, names: list[str], cfg: dict) -> None:
    b = win["bands"]
    med = {n: float(np.median(b[n][water])) for n in names}
    sigma = {n: max(1.4826 * float(np.median(np.abs(b[n][water] - med[n]))), cfg["water_sigma_floor"]) for n in names}
    win["water"] = {"mask": water, "median": med, "sigma": sigma}


def prepare(win: dict, water_cfg: dict, shape_bands: list[str], cfg: dict) -> dict | None:
    b = win["bands"]
    total = b["B03"] + b["B08"]
    valid = total > 0
    ndwi = np.where(valid, (b["B03"] - b["B08"]) / np.where(valid, total, 1), 0)
    found = block_threshold(ndwi, valid, water_cfg)
    if found is None:
        return None
    mode, threshold = found
    water = valid & (np.abs(ndwi - mode) <= mode - threshold)
    win["shape_bands"] = [n for n in shape_bands if n in b]
    win["excess_bands"] = [n for n in cfg["excess_bands"] if n in b]
    names = list(dict.fromkeys(win["shape_bands"] + list(cfg["profile_bands"]) + win["excess_bands"]))
    water_levels(win, water, names, cfg)
    for _ in range(int(cfg["water_clip_rounds"])):
        stats = pixel_stats(b, win)
        keep = water.copy()
        for stat in stats:
            centre = float(np.median(stat[water]))
            spread = max(1.4826 * float(np.median(np.abs(stat[water] - centre))), cfg["clip_spread_floor"])
            keep &= np.abs(stat - centre) <= cfg["water_clip_sigma"] * spread
        water = keep
        water_levels(win, water, names, cfg)
    angle, _, z = pixel_stats(b, win)
    win["water"].update(
        {
            "ndwi_mode": mode,
            "n": int(water.sum()),
            "angle_p99": float(np.percentile(angle[water], 99)),
            "z_cut": float(np.quantile(z[water], cfg["hull_excess_quantile"])),
        }
    )
    return win["water"]


def hull_threshold(win: dict, cfg: dict) -> tuple[float, float]:
    w = win["water"]
    return max(cfg["hull_angle_deg"], cfg["water_margin"] * w["angle_p99"]), cfg["water_margin"] * w["z_cut"]


def hull_cells(win: dict, arrays: dict, cfg: dict) -> np.ndarray:
    angle, _, z = pixel_stats(arrays, win)
    ta, tz = hull_threshold(win, cfg)
    return (angle > ta) | (z > tz)


def to_xy(win: dict, lon: float, lat: float) -> tuple[float, float]:
    xs, ys = warp_transform("EPSG:4326", win["crs"], [lon], [lat])
    return xs[0], ys[0]


def sample_array(
    win: dict, arr: np.ndarray, cx: float, cy: float, heading: float, s: np.ndarray, t: np.ndarray, order: int = 1
) -> np.ndarray:
    al, ac = axes(heading)
    gs, gt = np.meshgrid(s, t, indexing="ij")
    x = cx + gs * al[0] + gt * ac[0]
    y = cy + gs * al[1] + gt * ac[1]
    tr = win["transform"]
    col = (x - tr.c) / tr.a - 0.5
    row = (y - tr.f) / tr.e - 0.5
    return map_coordinates(arr, [row, col], order=order, mode="nearest")


def sample(
    win: dict, cx: float, cy: float, heading: float, s: np.ndarray, t: np.ndarray, names
) -> dict[str, np.ndarray]:
    return {n: sample_array(win, win["bands"][n], cx, cy, heading, s, t) for n in names}


def smooth_curve(v: np.ndarray, size: int) -> np.ndarray:
    return ndi.median_filter(v, size=max(1, int(size)), mode="nearest") if len(v) else v


def slice_curves(mask: np.ndarray, grid: dict, water: dict, cfg: dict) -> dict | None:
    step = cfg["grid_step_m"]
    rows_any = np.where(mask.any(axis=1))[0]
    if len(rows_any) == 0:
        return None
    r0, r1 = int(rows_any.min()), int(rows_any.max())
    per = max(1, int(round(cfg["slice_m"] / step)))
    starts = list(range(r0, r1 + 2 - per, per))
    sigma_cells = max(cfg["smooth_sigma_m"] / step, 0.0)
    out = {f"{n}_w": [] for n in cfg["profile_bands"]}
    for st in starts:
        sl = slice(st, st + per)
        for n in cfg["profile_bands"]:
            e = (grid[n][sl] - water["median"][n]).mean(axis=0)
            e = ndi.gaussian_filter1d(e, sigma_cells) if sigma_cells > 0 else e
            peak = float(e.max())
            if peak < cfg["min_peak_sigma"] * water["sigma"][n]:
                out[f"{n}_w"].append(float("nan"))
                continue
            sel = np.where(e >= cfg["excess_width_frac"] * peak)[0]
            out[f"{n}_w"].append((sel.max() - sel.min() + 1) * step)
    return {k: np.array(v, dtype=float) for k, v in out.items()}


def annulus_water(win: dict, fix: dict, blob_l: float, blob_w: float, cfg: dict) -> tuple[dict | None, dict]:
    tr = win["transform"]
    h, w = win["shape"]
    cols, rows = np.meshgrid(np.arange(w), np.arange(h))
    px = tr.c + (cols + 0.5) * tr.a
    py = tr.f + (rows + 0.5) * tr.e
    al, ac = axes(fix["heading"])
    along = (px - fix["x"]) * al[0] + (py - fix["y"]) * al[1]
    across = (px - fix["x"]) * ac[0] + (py - fix["y"]) * ac[1]
    dist = np.hypot(np.maximum(np.abs(along) - blob_l / 2, 0), np.maximum(np.abs(across) - blob_w / 2, 0))
    ann = (dist >= cfg["annulus_inner_m"]) & (dist <= cfg["annulus_outer_m"])
    b = win["bands"]
    ann &= (b["B03"] + b["B08"]) > 0
    stats = {"cells": int(ann.sum()), "cloud_share": None, "clear_cells": None}
    if win.get("scl") is not None:
        cloud = np.isin(win["scl"], cfg["cloud_classes"])
        stats["cloud_share"] = float((cloud & ann).sum() / max(ann.sum(), 1))
        ann &= ~np.isin(win["scl"], cfg["annulus_opaque_cloud_classes"])
    other = ndi.binary_dilation(hull_cells(win, b, cfg), iterations=int(cfg["annulus_exclude_cells"]))
    if (ann & ~other).sum() >= cfg["annulus_min_cells"]:
        ann &= ~other
    stats["clear_cells"] = int(ann.sum())
    if win.get("scl") is not None and ann.sum() < cfg["annulus_clear_min_cells"]:
        return None, stats
    if ann.sum() < cfg["annulus_min_cells"]:
        return None, stats
    swir = [n for n in cfg["swir_bands"] if n in b]
    names = list(dict.fromkeys(win["shape_bands"] + list(cfg["profile_bands"]) + win["excess_bands"] + swir))
    local = dict(win)
    water_levels(local, ann, names, cfg)
    local["excess_bands"] = swir

    def zmap() -> np.ndarray:
        wt = local["water"]
        return np.mean([(b[n] - wt["median"][n]) / wt["sigma"][n] for n in swir], axis=0)

    for _ in range(int(cfg["annulus_clip_rounds"])):
        z = zmap()
        centre = float(np.median(z[ann]))
        spread = max(1.4826 * float(np.median(np.abs(z[ann] - centre))), cfg["clip_spread_floor"])
        keep = ann & (np.abs(z - centre) <= cfg["annulus_clip_sigma"] * spread)
        if keep.sum() < cfg["annulus_min_cells"]:
            break
        ann = keep
        water_levels(local, ann, names, cfg)
    z_cut = float(np.quantile(zmap()[ann], cfg["hull_excess_quantile"]))
    local["water"].update({"angle_p99": 0.0, "z_cut": z_cut, "n": int(ann.sum())})
    return local, stats


def profiles(grid: dict, central: np.ndarray, water: dict, cfg: dict) -> dict:
    step = cfg["grid_step_m"]
    sigma_cells = max(cfg["smooth_sigma_m"] / step, 0.0)
    prof = {}
    for n in cfg["profile_bands"]:
        e = (grid[n][central] - water["median"][n]).mean(axis=0)
        prof[n] = ndi.gaussian_filter1d(e, sigma_cells) if sigma_cells > 0 else e
    return prof


def median_width(w: np.ndarray, cfg: dict) -> float | None:
    ok = ~np.isnan(w)
    if ok.sum() < 3:
        return None
    return float(np.median(smooth_curve(w[ok], cfg["width_smooth_slices"])))


def width_measure(curves: dict | None, beam: float, cfg: dict) -> dict:
    out = {"width_b08_m": None, "width_b11_m": None, "width_b11_corr_m": None, "width_m": None, "width_ratio": None}
    if curves is None:
        return out
    out["width_b08_m"] = median_width(curves["B08_w"], cfg)
    out["width_b11_m"] = median_width(curves["B11_w"], cfg)
    if out["width_b11_m"] is not None:
        out["width_b11_corr_m"] = math.sqrt(max(out["width_b11_m"] ** 2 - cfg["b11_blur_m"] ** 2, 0.0))
    found = [v for v in (out["width_b08_m"], out["width_b11_corr_m"]) if v is not None]
    if found:
        out["width_m"] = max(found)
        out["width_ratio"] = out["width_m"] / beam
    return out


def blob_geometry(sv: np.ndarray, tv: np.ndarray) -> dict:
    cov = np.cov(np.vstack([sv, tv])) if len(sv) > 2 else np.eye(2)
    vals, vecs = np.linalg.eigh(cov)
    major = vecs[:, 1]
    return {
        "s": float(sv.mean()),
        "t": float(tv.mean()),
        "length_m": math.sqrt(12 * max(vals[1], 1e-9)),
        "width_m": math.sqrt(12 * max(vals[0], 1e-9)),
        "axis_deg": math.degrees(math.atan2(abs(major[1]), abs(major[0]))),
    }
