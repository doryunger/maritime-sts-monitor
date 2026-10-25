import math

import numpy as np

from maritime_tip_cue.sts import confidence, s1shape
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


def vessels(obj: dict) -> list[dict] | None:
    par = (obj["d"] or {}).get("par")
    if par is None or not obj.get("crs"):
        return None
    from rasterio.warp import transform as warp_transform

    out = []
    for p0, p1 in par["ext"]:
        mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
        lon, lat = warp_transform(obj["crs"], "EPSG:4326", [float(mx)], [float(my)])
        out.append({"lat": lat[0], "lon": lon[0], "line_m": float(np.hypot(*(np.asarray(p1) - np.asarray(p0))))})
    return sorted(out, key=lambda v: -v["line_m"])


def fill(d: dict, pixel: float) -> float:
    _, length, width = d["rect"]
    return len(d["xy"]) * pixel * pixel / max(length * width, 1e-9)


def verdict(rec: dict, c: dict) -> str:
    length, wb, sep, ov, fl = rec["length_m"], rec["width_beams"], rec["light_separation"], rec["overlap"], rec.get("fill")
    if None in (length, wb, sep, ov, fl):
        return "single"
    base = (
        c["length_min_m"] <= length <= c["length_max_m"]
        and sep >= c["light_separation_min"]
        and wb <= c["width_max_beams"]
        and fl >= c["fill_min"]
    )
    lines = ov >= c["overlap_min"] or (ov >= c["overlap_min_boxy"] and fl >= c["fill_min_boxy"])
    if base and wb >= c["width_min_beams"] and lines:
        return "pair"
    if base and wb >= c["width_min_beams_possible"] and ov >= c["overlap_min_possible"]:
        return "possible_pair"
    return "single"


def evaluate(objects: list[dict], rules: dict) -> list[dict]:
    c = s1_config(rules)
    bl = blur(objects, c)
    conf = rules.get("s1_confidence")
    rows = []
    for obj in objects:
        rec = {"length_m": None, "width_beams": None, "light_separation": None, "overlap": None, "fill": None, "blur_m": bl, "vessels": None, "rect_angle_deg": None}
        d = obj["d"]
        if d is not None:
            rec["rect_angle_deg"] = float(d["rect"][0])
        if d is not None and obj["straight"]:
            _, length, width = d["rect"]
            rec.update(
                length_m=length,
                overlap=overlap(d, c["pixel_m"]),
                width_beams=(width - bl) / (length / c["hull_length_to_beam"]),
                light_separation=light_separation(obj, c["bright_quantile"]),
                fill=fill(d, c["pixel_m"]),
                vessels=vessels(obj),
            )
        v = verdict(rec, c)
        rule_pair = v == "pair"
        shape_cfg = rules.get("s1_single_box")
        if shape_cfg and shape_cfg["enabled"] and d is not None and obj["straight"]:
            rec["single_box"] = s1shape.measure(obj, shape_cfg, c["pixel_m"])
            if v in ("pair", "possible_pair") and rec["single_box"] and rec["single_box"]["single"]:
                v = "single"
                rec["single_box_veto"] = True
        rec["weak_evidence"] = v == "possible_pair"
        rec["verdict"] = c["weak_pair_verdict"] if v == "possible_pair" else v
        if conf and conf["enabled"]:
            rec["confidence"] = None
            if d is not None and obj["straight"] and rec["length_m"] is not None:
                shape_cfg = rules["s1_single_box"]
                reference = max(
                    max([x["length_m"] for x in obj.get("detections", []) if x.get("own") and x.get("length_m") is not None], default=0.0),
                    rec["length_m"] - bl,
                )
                pixel = c["pixel_m"]
                ratio = c["hull_length_to_beam"]
                vh = s1shape.second_hull(obj, shape_cfg, pixel, reference, "vh", ratio)
                vv = s1shape.second_hull(obj, shape_cfg, pixel, reference, "vv", ratio)
                rec["second_hull"] = {"vh_rows_share": (vh or {}).get("rows_share"), "vv_rows_share": (vv or {}).get("rows_share"), "vh_edge_noise": (vh or {}).get("edge_noise")}
                rec["rule_verdict"] = rec["verdict"]
                rec["shape_rule_pair"] = rule_pair
                rec["confidence"] = confidence.score(confidence.s1_features(rec, rule_pair, vh, vv, conf), conf)
                if conf["threshold"] is not None:
                    rec["verdict"] = "pair" if rec["confidence"] >= conf["threshold"] else "single"
        rows.append(rec)
    return rows
