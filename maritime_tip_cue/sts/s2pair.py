import math

import numpy as np
from scipy import ndimage as ndi
from scipy.signal import find_peaks

from maritime_tip_cue.sts import s2window


def clean(mask: np.ndarray, cells: int) -> np.ndarray:
    if cells <= 0:
        return mask
    mask = ndi.binary_closing(mask, structure=np.ones((3, 3)), iterations=int(cells))
    return ndi.binary_opening(mask, structure=np.ones((3, 3)), iterations=int(cells))


def pixel_frame(win: dict) -> tuple[np.ndarray, np.ndarray]:
    tr = win["transform"]
    h, w = win["shape"]
    cols, rows = np.meshgrid(np.arange(w), np.arange(h))
    return tr.c + (cols + 0.5) * tr.a, tr.f + (rows + 0.5) * tr.e


def heading_diff(major: np.ndarray, heading: float) -> float:
    return (math.degrees(math.atan2(major[0], major[1])) % 180 - heading % 180 + 90) % 180 - 90


def anchor_fit(win: dict, box: dict, cfg: dict) -> dict:
    mask = clean(s2window.hull_cells(win, win["bands"], cfg), cfg["mask_clean_cells"])
    px, py = pixel_frame(win)
    length, width = box["length_m"], box["width_m"]
    margin = max(cfg["anchor_margin_factor"] * width, cfg["anchor_min_margin_m"])
    al0, ac0 = s2window.axes(box["heading"])
    on_box = (np.abs((px - box["x"]) * al0[0] + (py - box["y"]) * al0[1]) <= length / 2) & (
        np.abs((px - box["x"]) * ac0[0] + (py - box["y"]) * ac0[1]) <= width / 2
    )
    x, y, heading = box["x"], box["y"], box["heading"]
    fix = {"x": x, "y": y, "heading": heading, "length_m": length, "source": "box", "half_pixels": 0}
    half_px = abs(win["transform"].a) / 2
    locked = (
        box.get("source") == "ridge"
        and cfg["recentre_lock_axis_ratio"] > 0
        and (box.get("ridge_axis_ratio") or 0) >= cfg["recentre_lock_axis_ratio"]
    )
    for _ in range(int(cfg["anchor_iterations"])):
        turn = 0.0
        al, ac = s2window.axes(heading)
        along = (px - x) * al[0] + (py - y) * al[1]
        across = (px - x) * ac[0] + (py - y) * ac[1]
        region = mask & (np.abs(along) <= length / 2 + margin) & (np.abs(across) <= width / 2 + margin)
        labels, count = ndi.label(region, structure=np.ones((3, 3)))
        if count == 0:
            break
        touching = np.unique(labels[on_box & region])
        touching = touching[touching > 0]
        if len(touching) == 0:
            break
        cells = np.isin(labels, touching)
        m = int(cfg["edge_margin_px"])
        at_edge = bool(cells[:m].any() or cells[-m:].any() or cells[:, :m].any() or cells[:, -m:].any())
        cx, cy = px[cells], py[cells]
        if len(cx) < 3:
            break
        vals, vecs = np.linalg.eigh(np.cov(np.vstack([cx, cy])))
        ratio = math.sqrt(vals[1] / max(vals[0], 1e-9))
        if ratio >= cfg["locate_min_axis_ratio"] and not locked:
            diff = heading_diff(vecs[:, 1], heading)
            heading = (heading + diff) % 360
            turn = abs(diff)
        al, ac = s2window.axes(heading)
        s_pix = (cx - x) * al[0] + (cy - y) * al[1]
        s_lo, s_hi = s_pix.min() - half_px, s_pix.max() + half_px
        sc = (s_lo + s_hi) / 2
        length_new = min(s_hi - s_lo, length + 2 * margin)
        x, y = x + sc * al[0], y + sc * al[1]
        length = max(length_new, box["length_m"] * cfg["anchor_min_length_frac"])
        fix = {
            "x": float(x),
            "y": float(y),
            "heading": float(heading),
            "length_m": float(length),
            "source": "pixels",
            "half_pixels": int(len(cx)),
            "axis_ratio": ratio,
            "heading_shift_deg": (heading - box["heading"] + 180) % 360 - 180,
            "at_edge": at_edge,
        }
        if turn < cfg["anchor_heading_tol_deg"] and abs(sc) < cfg["anchor_shift_tol_m"]:
            break
    if fix["length_m"] > cfg["max_hull_length_m"]:
        fix = dict(
            fix, x=box["x"], y=box["y"], length_m=min(box["length_m"], cfg["max_hull_length_m"]), source="clipped"
        )
    return fix


def recentre(win: dict, box: dict, cfg: dict) -> dict:
    beam = box["length_m"] / cfg["hull_length_to_beam"]
    reach = cfg["recentre_search_beams"] * beam
    out = dict(box, source="box")
    if reach <= 0:
        return out
    step = cfg["grid_step_m"]
    ring, _ = s2window.annulus_water(win, box, box["length_m"], box["width_m"] + 2 * cfg["anchor_strip_max_m"], cfg)
    water = ring["water"] if ring is not None else win["water"]
    names = [n for n in cfg["tenm_bands"] if n in win["bands"]]
    s = np.arange(-box["length_m"] / 2, box["length_m"] / 2 + step / 2, step)
    t = np.arange(-2 * reach - box["width_m"], 2 * reach + box["width_m"] + step / 2, step)
    grid = s2window.sample(win, box["x"], box["y"], box["heading"], s, t, names)
    z = np.max([np.clip((grid[n] - water["median"][n]) / water["sigma"][n], 0, None) for n in names], axis=0)
    central = np.abs(s) <= cfg["profile_along_frac"] * box["length_m"] / 2
    prof = z[central].mean(axis=0)
    sigma_cells = max(cfg["smooth_sigma_m"] / step, 0.0)
    if sigma_cells > 0:
        prof = ndi.gaussian_filter1d(prof, sigma_cells)
    near = np.where(np.abs(t) <= reach)[0]
    i0 = int(near[np.argmax(prof[near])])
    if prof[i0] < cfg["hull_b_k"]:
        return out
    lo, hi = i0, i0
    floor = cfg["excess_width_frac"] * prof[i0]
    while lo > 0 and prof[lo - 1] >= floor:
        lo -= 1
    while hi < len(prof) - 1 and prof[hi + 1] >= floor:
        hi += 1
    gs, gt = np.meshgrid(s, t, indexing="ij")
    sel = (gt >= t[lo] - step / 2) & (gt <= t[hi] + step / 2) & (z >= cfg["hull_b_k"])
    if sel.sum() < 3:
        sel = (gt >= t[lo] - step / 2) & (gt <= t[hi] + step / 2) & (z >= floor)
    al, ac = s2window.axes(box["heading"])
    cx = box["x"] + gs[sel] * al[0] + gt[sel] * ac[0]
    cy = box["y"] + gs[sel] * al[1] + gt[sel] * ac[1]
    w = z[sel]
    mx, my = float(np.average(cx, weights=w)), float(np.average(cy, weights=w))
    heading = box["heading"]
    ratio = None
    if len(cx) >= 3:
        vals, vecs = np.linalg.eigh(np.cov(np.vstack([cx, cy]), aweights=w))
        ratio = math.sqrt(vals[1] / max(vals[0], 1e-9))
        if ratio >= cfg["locate_min_axis_ratio"]:
            heading = (heading + heading_diff(vecs[:, 1], heading)) % 360
    out.update({"x": mx, "y": my, "heading": heading, "source": "ridge", "ridge_axis_ratio": ratio})
    return out


def a_extent(prof: dict, t: np.ndarray, water: dict, centre_half: float, cfg: dict):
    step = cfg["grid_step_m"]
    z = np.max([prof[n] / water["sigma"][n] for n in cfg["hull_b_bands"]], axis=0)
    near = np.where(np.abs(t) <= centre_half)[0]
    if len(near) == 0:
        return None
    i0 = int(near[np.argmax(z[near])])
    if z[i0] < cfg["hull_b_k"]:
        return None
    lo, hi = i0, i0
    while lo > 0 and z[lo - 1] >= cfg["hull_b_k"]:
        lo -= 1
    while hi < len(z) - 1 and z[hi + 1] >= cfg["hull_b_k"]:
        hi += 1
    a_lo, a_hi = lo, hi
    for n in cfg["hull_b_bands"]:
        zn = prof[n] / water["sigma"][n]
        peaks, _ = find_peaks(zn[lo : hi + 1], distance=max(1, int(cfg["min_peak_sep_m"] / step)))
        peaks = [int(p) + lo for p in peaks]
        if not peaks:
            continue
        own = min(peaks, key=lambda p: abs(p - i0))
        k = peaks.index(own)
        for side, idx in ((-1, k - 1), (1, k + 1)):
            if 0 <= idx < len(peaks):
                a, b = sorted((own, peaks[idx]))
                dip = int(np.argmin(zn[a : b + 1])) + a
                if 1 - zn[dip] / min(zn[own], zn[peaks[idx]]) >= cfg["seam_min_depth"]:
                    if side < 0 and dip > a_lo:
                        a_lo = dip
                    if side > 0 and dip < a_hi:
                        a_hi = dip
    return float(t[a_lo] - step / 2), float(t[a_hi] + step / 2), float(t[i0])


def best_run(res: np.ndarray, thr: float, step: float, cfg: dict) -> dict | None:
    best = None
    i = 0
    while i < len(res):
        if res[i] > thr:
            j = i
            while j + 1 < len(res) and res[j + 1] > thr:
                j += 1
            width, mean = (j - i + 1) * step, float(np.mean(res[i : j + 1]))
            if width >= cfg["hull_b_min_width_m"] and mean >= cfg["f4_min_residual_sigma"]:
                if best is None or mean > best["mean"]:
                    best = {"width_m": width, "mean": mean}
            i = j + 1
        else:
            i += 1
    return best


def slice_asymmetry(e: np.ndarray, cfg: dict) -> dict:
    step = cfg["grid_step_m"]
    e = np.clip(e, 0, None)
    i0 = int(np.argmax(e))
    m = min(i0, len(e) - 1 - i0)
    out = {"side": 0, "mean": 0.0, "width_m": 0.0}
    if m < 1:
        return out
    res = e[i0 + 1 : i0 + m + 1] - e[i0 - m : i0][::-1]
    thr = cfg["f4_k_res"] * math.sqrt(2.0)
    plus, minus = best_run(res, thr, step, cfg), best_run(-res, thr, step, cfg)
    pick = [(r["mean"], side, r) for side, r in ((1, plus), (-1, minus)) if r is not None]
    if pick:
        mean, side, r = max(pick, key=lambda x: x[0])
        out.update({"side": side, "mean": mean, "width_m": r["width_m"]})
    return out


def asym_evidence(
    grid: dict, s: np.ndarray, t: np.ndarray, water: dict, t_range: tuple[float, float], length_a: float, cfg: dict
) -> dict:
    step = cfg["grid_step_m"]
    per = max(1, int(round(cfg["f4_slice_m"] / step)))
    keep = np.where((t >= t_range[0]) & (t <= t_range[1]))[0]
    half = cfg["f4_along_frac"] * length_a / 2
    rows = np.where(np.abs(s) <= half)[0]
    starts = list(range(int(rows.min()), int(rows.max()) + 2 - per, per)) if len(rows) else []
    sigma_cells = max(cfg["smooth_sigma_m"] / step, 0.0)
    bands = {}
    for n in cfg["tenm_bands"]:
        if n not in grid:
            continue
        found = []
        for st in starts:
            e = ((grid[n][st : st + per][:, keep] - water["median"][n]) / water["sigma"][n]).mean(axis=0)
            e = ndi.gaussian_filter1d(e, sigma_cells) if sigma_cells > 0 else e
            found.append(slice_asymmetry(e, cfg))
        shares = {side: (float(np.mean([f["side"] == side for f in found])) if found else 0.0) for side in (1, -1)}
        side = max(shares, key=shares.get)
        hit = [f for f in found if f["side"] == side]
        bands[n] = {"share": shares[side], "mean": float(np.mean([f["mean"] for f in hit])) if hit else 0.0}
    share = 0.0
    if bands:
        best = max(bands, key=lambda n: (bands[n]["share"], bands[n]["mean"]))
        share = bands[best]["share"]
    return {"share": share, "evidence": share >= cfg["f4_slice_share"] and len(starts) > 0}


def find_b(
    grid: dict, gs: np.ndarray, gt: np.ndarray, water: dict, edges: tuple[float, float], length_a: float, cfg: dict
) -> dict:
    step = cfg["grid_step_m"]
    z = np.max([(grid[n] - water["median"][n]) / water["sigma"][n] for n in cfg["hull_b_bands"]], axis=0)
    hull = clean(z > cfg["hull_b_k"], cfg["mask_clean_cells"])
    along = np.abs(gs) <= length_a / 2 + cfg["anchor_overlap_m"]
    found = {}
    for side in (1, -1):
        edge = edges[1] if side > 0 else -edges[0]
        inner, outer = edge + cfg["anchor_strip_gap_m"], edge + cfg["anchor_strip_max_m"]
        strip = along & (side * gt >= inner) & (side * gt <= outer)
        labels, count = ndi.label(hull & strip, structure=np.ones((3, 3)))
        best = None
        for lab in range(1, count + 1):
            comp = labels == lab
            if comp.sum() * step * step < cfg["min_blob_area_m2"]:
                continue
            geo = s2window.blob_geometry(gs[comp], gt[comp])
            fits = (
                cfg["hull_b_min_width_m"] <= geo["width_m"] <= cfg["hull_b_max_width_m"]
                and geo["length_m"] >= cfg["hull_b_length_frac"] * length_a
                and geo["length_m"] / geo["width_m"] >= cfg["hull_b_min_elongation"]
                and geo["axis_deg"] <= cfg["hull_b_max_axis_deg"]
            )
            if not fits:
                continue
            rows = np.where(np.any(comp, axis=1))[0]
            cols = np.where(np.any(comp, axis=0))[0]
            across = z[rows.min() : rows.max() + 1, cols.min() : cols.max() + 1].mean(axis=0)
            n = max(2, int(round(cfg["hull_b_min_width_m"] / step)))
            levels = [
                across[i : i + n].min()
                for i in range(len(across) - n + 1)
                if across[i : i + n].min() / across[i : i + n].max() >= cfg["hull_b_plateau_frac"]
            ]
            level = float(max(levels)) if levels else 0.0
            geo["plateau_sigma"] = level
            if level < cfg["hull_b_plateau_min_sigma"]:
                continue
            if geo["length_m"] > cfg["max_hull_length_m"] or geo["length_m"] < cfg["min_length_m"]:
                continue
            geo.update(
                {
                    "cells": int(comp.sum()),
                    "t_lo": float(gt[comp].min() - step / 2),
                    "t_hi": float(gt[comp].max() + step / 2),
                    "side": side,
                }
            )
            if best is None or geo["cells"] > best["cells"]:
                best = geo
        if best is not None:
            found[side] = best
    return found


def halo(zprof: np.ndarray, t: np.ndarray, a: tuple[float, float], b: dict, cfg: dict) -> bool:
    a_sel = (t >= a[0]) & (t <= a[1])
    b_sel = (t >= b["t_lo"]) & (t <= b["t_hi"])
    centre = (a[0] + a[1]) / 2
    m_sel = (t >= 2 * centre - b["t_hi"]) & (t <= 2 * centre - b["t_lo"])
    peak_a = float(zprof[a_sel].max()) if a_sel.any() else 0.0
    b_to_a = float(zprof[b_sel].max()) / peak_a if peak_a > 0 and b_sel.any() else 1.0
    mirror = float(zprof[m_sel].mean()) if m_sel.any() else 0.0
    ratio = float(zprof[b_sel].mean()) / mirror if mirror > 0 and b_sel.any() else float("inf")
    return b_to_a < cfg["halo_max_b_to_a"] and ratio < cfg["halo_min_mirror_ratio"]


def empty_result(out: dict, verdict: str) -> dict:
    out.update({"hull_a": None, "hull_b": None, "b_fired": False, "verdict": verdict})
    return out


def blank_result(verdict: str, reason: str) -> dict:
    return {
        "verdict": verdict,
        "unmeasured_reason": reason,
        "a_fired": False,
        "b_fired": False,
        "width_ratio": None,
        "width_m": None,
        "ratio_ab": None,
        "b_offset_m": None,
        "hull_a": None,
        "hull_b": None,
    }


def check(win: dict, box: dict, cfg: dict) -> dict:
    base_water = win["water"]
    names = win["shape_bands"]
    start = recentre(win, box, cfg)
    fix = anchor_fit(win, start, cfg)
    if "heading_shift_deg" in fix:
        fix["heading_shift_deg"] = (fix["heading"] - box["heading"] + 180) % 360 - 180
    length_a = fix["length_m"]
    step = cfg["grid_step_m"]
    half_t = cfg["anchor_grid_half_m"]
    s = np.arange(-length_a / 2 - cfg["anchor_overlap_m"], length_a / 2 + cfg["anchor_overlap_m"] + step / 2, step)
    t = np.arange(-half_t, half_t + step / 2, step)
    allnames = list(dict.fromkeys(names + list(cfg["profile_bands"]) + win["excess_bands"]))
    grid = s2window.sample(win, fix["x"], fix["y"], fix["heading"], s, t, allnames)
    gs, gt = np.meshgrid(s, t, indexing="ij")
    ring, ring_stats = s2window.annulus_water(win, fix, length_a, box["width_m"] + 2 * cfg["anchor_strip_max_m"], cfg)
    water = ring["water"] if ring is not None else base_water
    central = np.abs(s) <= cfg["profile_along_frac"] * length_a / 2
    prof = s2window.profiles(grid, central, water, cfg)
    shifted = abs(fix.get("heading_shift_deg", 0.0)) >= cfg["beam_measured_shift_deg"]
    beam = (fix["length_m"] if shifted else box["length_m"]) / cfg["hull_length_to_beam"]
    mask = clean(s2window.hull_cells(win, grid, cfg), cfg["mask_clean_cells"])
    edges = a_extent(prof, t, water, box["width_m"] / 2, cfg)
    out = {
        "fix": dict(fix),
        "unmeasured_reason": None,
        "width_ratio": None,
        "width_m": None,
        "a_fired": False,
        "b_fired": False,
        "ratio_ab": None,
        "b_offset_m": None,
        "hull_a": None,
        "hull_b": None,
        "cloud_beside_share": ring_stats["cloud_share"],
        "reaches_water": True,
    }
    if ring is None and win.get("scl") is not None and ring_stats["clear_cells"] < cfg["annulus_clear_min_cells"]:
        out["unmeasured_reason"] = (
            f"cloud: only {ring_stats['clear_cells']} clear water pixels around the hull "
            f"({ring_stats['cloud_share']:.0%} of the surroundings are cloud)"
        )
        return empty_result(out, "unmeasured")
    if fix.get("at_edge"):
        out["unmeasured_reason"] = "hull A runs out of the image window, so its length and beam are not measured"
        return empty_result(out, "unmeasured")
    if edges is None:
        return empty_result(out, "single")
    a_lo, a_hi, centre = edges
    zprof = np.max([prof[n] / water["sigma"][n] for n in cfg["hull_b_bands"]], axis=0)
    cap = cfg["anchor_cap_factor"] * beam
    if a_hi - a_lo > cap:
        a_lo, a_hi = max(a_lo, centre - cap / 2), min(a_hi, centre + cap / 2)
    lo_strip = (a_lo - cfg["anchor_strip_max_m"], a_lo - cfg["anchor_strip_gap_m"])
    hi_strip = (a_hi + cfg["anchor_strip_gap_m"], a_hi + cfg["anchor_strip_max_m"])
    out["hull_a"] = {"t_m": (a_lo + a_hi) / 2, "width_m": a_hi - a_lo, "s_m": 0.0, "length_m": length_a}
    dry = {}
    for side, outer in (("+t", a_hi + cfg["anchor_strip_max_m"]), ("-t", a_lo - cfg["anchor_strip_max_m"])):
        idx = int(np.argmin(np.abs(t - outer)))
        reach = t[0] <= outer <= t[-1] and all(
            float(prof[n][idx]) <= cfg["water_end_sigma"] * water["sigma"][n] for n in cfg["profile_bands"]
        )
        dry[side] = not reach
    keep = (t >= lo_strip[0]) & (t <= hi_strip[1])
    curves = s2window.slice_curves(mask[:, keep], {n: g[:, keep] for n, g in grid.items()}, water, cfg)
    out.update(s2window.width_measure(curves, beam, cfg))
    wide = out["width_ratio"] is not None and out["width_ratio"] >= cfg["pair_width_ratio"]
    asym = asym_evidence(grid, s, t, water, (lo_strip[0], hi_strip[1]), length_a, cfg)
    f4_length = asym["share"] * length_a
    f4_pair = bool(wide and asym["evidence"] and f4_length >= cfg["f4_min_length_m"])
    out["a_fired"] = bool(wide)
    found = find_b(grid, gs, gt, water, (a_lo, a_hi), length_a, cfg)
    for side in list(found):
        if halo(zprof, t, (a_lo, a_hi), found[side], cfg):
            del found[side]
    if not found and all(dry.values()):
        out["reaches_water"] = False
        out["unmeasured_reason"] = "profile does not reach water at either strip edge"
        return empty_result(out, "unmeasured")
    if found:
        side, b = max(found.items(), key=lambda kv: kv[1]["cells"])
        lo = min([a_lo] + [v["t_lo"] for v in found.values()])
        hi = max([a_hi] + [v["t_hi"] for v in found.values()])
        out["ratio_ab"] = (hi - lo) / beam
        out["hull_b"] = {
            "t_m": (b["t_lo"] + b["t_hi"]) / 2,
            "s_m": b["s"],
            "width_m": b["width_m"],
            "length_m": b["length_m"],
            "plateau_sigma": b["plateau_sigma"],
            "side": side,
        }
        out["b_offset_m"] = abs(out["hull_b"]["t_m"] - out["hull_a"]["t_m"])
        out["b_fired"] = True
        verdict = "pair" if out["ratio_ab"] >= cfg["pair_width_ratio"] or f4_pair else "possible_pair"
    else:
        verdict = "pair" if f4_pair else "single"
    out["verdict"] = verdict
    return out


def check_hull(win: dict, hull: dict, cfg: dict) -> dict:
    speed = hull.get("speed_kn")
    if speed is not None and speed > cfg["stopped_speed_kn"]:
        limit = cfg["stopped_speed_kn"]
        return blank_result("moving", f"Ai2 speed {speed:.1f} kn is above the stopped limit {limit:g} kn")
    x, y = s2window.to_xy(win, hull["lon"], hull["lat"])
    box = {"x": x, "y": y, "length_m": hull["length_m"], "width_m": hull["width_m"], "heading": hull["heading"]}
    return check(win, box, cfg)


def run(win: dict | None, hull: dict, rules: dict, shape_bands: list[str]) -> dict:
    cfg = rules["pair_split"]
    if win is None or s2window.prepare(win, rules["sensing_water"], shape_bands, cfg) is None:
        return blank_result("unmeasured", "no water peak in the window")
    return check_hull(win, hull, cfg)
