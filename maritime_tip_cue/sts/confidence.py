import math

S1_NAMES = ("in_length", "shape_rule", "width_off", "light_sep", "fill", "vh_second", "vv_second", "vh_edge_noise")
S2_NAMES = (
    "rule_pair",
    "weak_evidence",
    "b_fired",
    "width_ratio",
    "ratio_ab",
    "plateau_sigma",
    "shape_step_k",
    "shape_continuity",
    "shape_pair",
    "shape_veto",
)


def s1_features(rec: dict, rule_pair_before_veto: bool, evidence_vh: dict | None, evidence_vv: dict | None, cfg: dict) -> dict:
    length = rec["length_m"]
    vh, vv = evidence_vh or {}, evidence_vv or {}
    return {
        "in_length": float(cfg["length_min_m"] <= length <= cfg["length_max_m"]),
        "shape_rule": float(bool(rule_pair_before_veto)),
        "width_off": abs(rec["width_beams"] - cfg["width_centre_beams"]),
        "light_sep": rec["light_separation"] or 0.0,
        "fill": rec["fill"] or 0.0,
        "vh_second": vh.get("rows_share") or 0.0,
        "vv_second": vv.get("rows_share") or 0.0,
        "vh_edge_noise": math.log1p(vh.get("edge_noise") or 0.0),
    }


def s2_features(check: dict, verdict: str, cfg: dict) -> dict:
    return {
        "rule_pair": float(verdict == "pair"),
        "weak_evidence": float(bool(check.get("weak_evidence"))),
        "b_fired": float(bool(check.get("b_fired"))),
        "width_ratio": min(check.get("width_ratio") or 0.0, cfg["width_ratio_cap"]),
        "ratio_ab": min(check.get("ratio_ab") or 0.0, cfg["ratio_ab_cap"]),
        "plateau_sigma": math.log1p(check.get("plateau_sigma") or 0.0),
        "shape_step_k": math.log1p(max(check.get("shape_step_k") or 0.0, 0.0)),
        "shape_continuity": check.get("shape_continuity") or 0.0,
        "shape_pair": float(check.get("shape_call") == "pair"),
        "shape_veto": float(check.get("shape_call") == "veto"),
    }


def score(features: dict, cfg: dict) -> float:
    z = cfg["intercept"] + sum(cfg[f"coef_{name}"] * value for name, value in features.items())
    return 1.0 / (1.0 + math.exp(-z))
