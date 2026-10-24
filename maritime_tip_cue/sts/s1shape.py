import math

import numpy as np
from scipy.ndimage import map_coordinates
from scipy.signal import find_peaks

from maritime_tip_cue.sts.s2pair import half_crossing


def measure(obj: dict, cfg: dict, pixel: float) -> dict | None:
    d = obj["d"]
    own = [v for v in obj.get("detections", []) if v.get("own")]
    if d is None or not own or len(own) > cfg["max_own_detections"]:
        return None
    lengths = [v["length_m"] for v in own if v.get("length_m") is not None]
    widths = [v["width_m"] for v in own if v.get("width_m") is not None]
    if not lengths or not widths:
        return None
    length, beam = max(lengths), max(widths)
    if not np.isfinite([length, beam]).all() or min(length, beam) <= 0:
        return None
    angle, _, width = d["rect"]
    u = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])
    n = np.array([-u[1], u[0]])
    along, across = d["xy"] @ u, d["xy"] @ n
    center = u * (along.min() + along.max()) / 2 + n * (across.min() + across.max()) / 2
    step = cfg["grid_step_m"]
    s = np.arange(-length / 2, length / 2 + step / 2, step)
    reach = width / 2 + cfg["sea_band_m"]
    t = np.arange(-reach, reach + step / 2, step)
    x = center[0] + s[:, None] * u[0] + t[None, :] * n[0]
    y = center[1] + s[:, None] * u[1] + t[None, :] * n[1]
    a = obj["a"]
    rr, cc = (a["py"][0, 0] - y) / pixel, (x - a["px"][0, 0]) / pixel
    power = 10 ** (a[cfg["polarisation"]] / 10)
    grid = map_coordinates(power, np.array([rr, cc]), order=1, mode="constant", cval=np.nan)
    near = np.abs(t) <= width / 2 + cfg["search_margin_m"]
    sea_cells = np.abs(t) >= width / 2 + cfg["sea_margin_m"]
    rows = []
    profiles = []
    central = np.where(np.abs(s) <= cfg["along_fraction"] * length / 2)[0]
    for i in central:
        profile = grid[i]
        if not np.isfinite(profile[near]).all():
            return None
        sea = profile[sea_cells & np.isfinite(profile)]
        if len(sea) < cfg["min_sea_cells"]:
            continue
        median = np.median(sea)
        sigma = 1.4826 * np.median(np.abs(sea - median))
        excess = profile - median
        indices = np.where(near & np.isfinite(profile))[0]
        if not len(indices):
            continue
        peak = indices[np.argmax(excess[indices])]
        if excess[peak] <= 0:
            continue
        lo, gl = half_crossing(excess, peak, excess[peak] * cfg["edge_fraction"], -1)
        hi, gh = half_crossing(excess, peak, excess[peak] * cfg["edge_fraction"], 1)
        if lo is None or hi is None or not np.isfinite([lo, hi, gl, gh]).all():
            continue
        sl = math.hypot(step * sigma / max(gl, 1e-9), pixel / math.sqrt(12))
        sh = math.hypot(step * sigma / max(gh, 1e-9), pixel / math.sqrt(12))
        rows.append([s[i], t[0] + lo * step, t[0] + hi * step, sl, sh, excess[peak] / max(sigma, 1e-9)])
        profiles.append((excess, sigma))
    if len(rows) < cfg["min_rows"] or len(rows) < cfg["min_row_fraction"] * len(central):
        return None
    ss, lo, hi, sl, sh, peaks = np.asarray(rows).T
    count = len(ss)
    design = np.column_stack([
        np.r_[ss, ss],
        np.r_[np.ones(count), np.zeros(count)],
        np.r_[np.zeros(count), np.ones(count)],
    ])
    values, uncertainty = np.r_[lo, hi], np.r_[sl, sh]
    coef = np.linalg.lstsq(design / uncertainty[:, None], values / uncertainty, rcond=None)[0]
    residual = values - design @ coef
    fitted_width = float((coef[2] - coef[1]) / math.sqrt(1 + coef[0] ** 2))
    edge_noise = float(np.sqrt(np.mean((residual / uncertainty) ** 2)))
    width_ratio = fitted_width / beam
    peak_sigma = float(peaks.min())
    outside_rows = 0
    for position, (excess, sigma) in zip(ss, profiles):
        finite = np.where(np.isfinite(excess), excess, -np.inf)
        candidates, _ = find_peaks(finite, prominence=cfg["outside_prominence_sigma"] * sigma)
        left, right = coef[0] * position + coef[1], coef[0] * position + coef[2]
        supported = [
            k for k in candidates
            if near[k] and (t[k] < left or t[k] > right) and excess[k] >= cfg["outside_peak_sigma"] * sigma
        ]
        outside_rows += bool(supported)
    single = (
        peak_sigma >= cfg["min_peak_sigma"]
        and edge_noise <= cfg["max_edge_noise"]
        and cfg["min_width_ratio"] <= width_ratio <= cfg["max_width_ratio"]
        and outside_rows <= cfg["max_outside_peak_rows"]
    )
    fitted_angle = angle + math.degrees(math.atan(coef[0]))
    direction = np.array([math.cos(math.radians(fitted_angle)), math.sin(math.radians(fitted_angle))])
    normal = np.array([-direction[1], direction[0]])
    offset = float((coef[1] + coef[2]) / 2)
    midpoint = center + n * offset
    corners = [
        (midpoint + direction * al * length / 2 + normal * ac * fitted_width / 2).tolist()
        for al, ac in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]
    return {
        "single": bool(single),
        "edge_noise": edge_noise,
        "width_m": fitted_width,
        "width_ratio": width_ratio,
        "reference_length_m": float(length),
        "ai2_width_m": float(beam),
        "support_length_m": float(np.ptp(ss)),
        "rows": count,
        "peak_sigma": peak_sigma,
        "outside_peak_rows": outside_rows,
        "center": center.tolist(),
        "angle_deg": float(fitted_angle),
        "offset_m": offset,
        "corners": corners,
    }
