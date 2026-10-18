import math

import numpy as np

from maritime_tip_cue.sts.rules import s1_config


def blur(objects: list[dict], c: dict) -> float:
    cand = [o["d"]["rect"] for o in objects if o["d"] is not None]
    if not cand:
        return float("nan")
    ranked = sorted(cand, key=lambda r: r[2] / r[1])
    sel = ranked[: max(1, int(round(c["narrow_fraction"] * len(ranked))))]
    return float(np.median([r[2] - r[1] / c["hull_length_to_beam"] for r in sel]))


def overlap(d: dict, pixel: float) -> float | None:
    par = d.get("par")
    if par is None:
        return None
    t = d["rect"][0]
    u = np.array([math.cos(math.radians(t)), math.sin(math.radians(t))])
    iv = [sorted((float(p0 @ u), float(p1 @ u))) for p0, p1 in par["ext"]]
    e = [(hi - lo) + pixel for lo, hi in iv]
    return max(0.0, min(iv[0][1], iv[1][1]) - max(iv[0][0], iv[1][0])) / min(e)


def light_separation(obj: dict, quantile: float) -> float | None:
    d = obj["d"]
    t, _, width = d["rect"]
    n = np.array([-math.sin(math.radians(t)), math.cos(math.radians(t))])
    v = obj["a"][obj["pol"]][d["rr"], d["cc"]]
    keep = np.argsort(np.argsort(v)) / max(len(v) - 1, 1) >= quantile
    q, w = d["xy"][keep] @ n, 10 ** (v[keep] / 10)
    order = np.argsort(q)
    q, w = q[order], w[order]
    if len(q) < 4 or width <= 0:
        return None
    cw, cm = np.cumsum(w), np.cumsum(w * q)
    best, sep = -1.0, None
    for i in range(1, len(q)):
        w0, w1 = cw[i - 1], cw[-1] - cw[i - 1]
        if w1 <= 0:
            break
        m0, m1 = cm[i - 1] / w0, (cm[-1] - cm[i - 1]) / w1
        b = w0 * w1 * (m1 - m0) ** 2
        if b > best:
            best, sep = b, m1 - m0
    return None if sep is None else float(sep / width)


def verdict(rec: dict, c: dict) -> str:
    length, wb, sep, ov = rec["length_m"], rec["width_beams"], rec["light_separation"], rec["overlap"]
    flags = (
        length is not None and c["length_min_m"] <= length <= c["length_max_m"],
        wb is not None and wb >= c["width_min_beams"],
        sep is not None and sep >= c["light_separation_min"],
        ov is not None and ov >= c["overlap_min"],
    )
    return "pair" if all(flags) else "single"


def evaluate(objects: list[dict], rules: dict) -> list[dict]:
    c = s1_config(rules)
    bl = blur(objects, c)
    rows = []
    for obj in objects:
        rec = {"length_m": None, "width_beams": None, "light_separation": None, "overlap": None, "blur_m": bl}
        d = obj["d"]
        if d is not None and obj["straight"]:
            _, length, width = d["rect"]
            rec.update(
                length_m=length,
                overlap=overlap(d, c["pixel_m"]),
                width_beams=(width - bl) / (length / c["hull_length_to_beam"]),
                light_separation=light_separation(obj, c["bright_quantile"]),
            )
        rec["verdict"] = verdict(rec, c)
        rows.append(rec)
    return rows
