import re
import sys
from datetime import UTC, datetime

import pycountry
import requests

from maritime_tip_cue.equasis.client import Equasis, NotConfigured, SessionExpired, configured
from maritime_tip_cue.watch.state import Store

FIELDS = (
    "imo", "mmsi", "call_sign", "gross_tonnage", "dwt", "year_built", "vessel_type", "status", "equasis_name",
    "equasis_flag", "false_flag", "confidence", "reasons", "candidates", "source", "resolved_utc",
)


def number(text) -> float | None:
    found = re.search(r"\d[\d,]*", str(text or ""))
    return float(found.group().replace(",", "")) if found else None


def alpha2(equasis_flag: str) -> str:
    found = re.search(r"\(([A-Z]{3})\)", equasis_flag or "")
    country = pycountry.countries.get(alpha_3=found.group(1)) if found else None
    if country:
        return country.alpha_2
    name = re.sub(r"\s*\(.*\)|\bFalse\b", "", equasis_flag or "").strip()
    try:
        return pycountry.countries.lookup(name).alpha_2 if name else ""
    except LookupError:
        return ""


def false_flag(equasis_flag: str) -> bool:
    return bool(re.search(r"\bFalse\b", equasis_flag or ""))


def kind(vessel_type: str, rules: dict) -> str:
    text = (vessel_type or "").upper()
    if any(word in text for word in rules["tanker_words"]):
        return "8"
    if any(word in text for word in rules["cargo_words"]):
        return "7"
    return ""


def score(ours: dict, candidate: dict, rules: dict) -> tuple[int, list[str]]:
    points, why = 0, []
    if ours.get("flag") and alpha2(candidate["flag"]) == ours["flag"]:
        points += rules["scores"]["flag"]
        why.append("flag")
    if ours.get("type_code") and kind(candidate["type"], rules) == str(ours["type_code"]):
        points += rules["scores"]["type"]
        why.append("type")
    tonnage = number(candidate.get("gross_tonnage"))
    if tonnage and ours.get("dwt"):
        ratio = tonnage / ours["dwt"]
        if rules["gt_per_dwt"]["min"] <= ratio <= rules["gt_per_dwt"]["max"]:
            points += rules["scores"]["size"]
            why.append("size")
    return points, why


def level(points: int, rules: dict) -> str:
    if points >= rules["levels"]["strong"]:
        return "strong"
    return "medium" if points >= rules["levels"]["medium"] else "weak"


def close_dwt(equasis_dwt, ours_dwt, rules: dict) -> bool:
    dwt = number(equasis_dwt)
    return bool(dwt and ours_dwt and abs(dwt - ours_dwt) <= rules["dwt_tolerance"] * dwt)


def resolve_vessel(eq: Equasis, ours: dict, rules: dict) -> dict:
    candidates = eq.search(ours["name"])
    ranked = sorted(((score(ours, c, rules), c) for c in candidates), key=lambda item: -item[0][0])
    tied = [item for item in ranked if item[0][0] == ranked[0][0][0]] if ranked else []
    if len(tied) > 1 and ours.get("dwt"):
        rescored = []
        for (points, why), candidate in tied[:3]:
            if close_dwt(eq.details(candidate["imo"]).get("dwt"), ours["dwt"], rules):
                points, why = points + rules["scores"]["size"], why + ["dwt"]
            rescored.append(((points, why), candidate))
        ranked = sorted(rescored, key=lambda item: -item[0][0]) + ranked[len(tied):]
    record = {"candidates": len(candidates), "source": "equasis", "resolved_utc": datetime.now(UTC).isoformat(timespec="seconds")}
    tie = len(ranked) > 1 and ranked[0][0][0] == ranked[1][0][0]
    if not ranked or tie or level(ranked[0][0][0], rules) == "weak":
        return record | {"confidence": "unresolved" if ranked else "not found", "reasons": "tie" if tie else ""}
    (points, why), best = ranked[0]
    details = eq.details(best["imo"])
    record |= {
        "imo": best["imo"],
        "confidence": level(points, rules),
        "reasons": "+".join(why),
        "equasis_name": best["name"],
        "equasis_flag": best["flag"],
        "false_flag": int(false_flag(best["flag"])),
        "vessel_type": best["type"],
        "gross_tonnage": best["gross_tonnage"],
        "year_built": best["year_built"],
        "call_sign": details.get("call_sign", ""),
        "mmsi": details.get("mmsi", ""),
        "status": details.get("status", ""),
        "dwt": details.get("dwt", ""),
    }
    if close_dwt(details.get("dwt"), ours.get("dwt"), rules):
        record["reasons"] += "" if "dwt" in record["reasons"] else "+dwt"
        record["confidence"] = "strong"
    return record


def pending(store: Store) -> list[dict]:
    rows = store.db.execute(
        "select m.ship_id, m.name, m.flag, m.ais_length_m as length_m, c.type_code, c.dwt "
        "from ais_matches m left join ais_candidates c on c.scene_id = m.scene_id and c.site = m.site "
        "and c.hull_id = m.hull_id and c.slot = m.slot and c.ship_id = m.ship_id "
        "left join vessel_identities v on v.ship_id = m.ship_id "
        "where m.chosen = 1 and m.ship_id is not null and m.name is not null and v.ship_id is null "
        "order by m.scene_id, m.hull_id, m.slot"
    ).fetchall()
    seen, vessels = set(), []
    for row in rows:
        if row["ship_id"] not in seen:
            seen.add(row["ship_id"])
            vessels.append(dict(row))
    return vessels


def save_identity(store: Store, ship_id: str, record: dict) -> None:
    values = {field: record.get(field) for field in FIELDS}
    store.db.execute(
        f"insert or replace into vessel_identities (ship_id, {', '.join(FIELDS)}) values (?{', ?' * len(FIELDS)})",
        (ship_id, *values.values()),
    )
    if record.get("imo"):
        store.db.execute(
            "update ais_matches set imo = coalesce(imo, ?), mmsi = coalesce(mmsi, nullif(?, '')) where ship_id = ? and chosen = 1",
            (record["imo"], record.get("mmsi"), ship_id),
        )


def enrich(store: Store, rules: dict, eq: Equasis | None = None, log=print) -> dict:
    vessels = pending(store)
    summary = {"looked_up": 0, "resolved": 0, "unresolved": 0, "skipped": None}
    if not vessels:
        return summary
    limit = rules.get("max_lookups_per_run")
    if limit:
        vessels = vessels[: int(limit)]
    if eq is None:
        if not configured():
            summary["skipped"] = "Equasis credentials are not set"
            return summary
        eq = Equasis(rules["request_delay_s"])
    for vessel in vessels:
        try:
            record = resolve_vessel(eq, vessel, rules)
        except (SessionExpired, NotConfigured, requests.RequestException) as error:
            summary["skipped"] = f"{type(error).__name__}: {error}"
            log(f"equasis lookup stopped: {summary['skipped']}", file=sys.stderr)
            break
        with store.tx():
            save_identity(store, vessel["ship_id"], record)
        summary["looked_up"] += 1
        summary["resolved" if record.get("imo") else "unresolved"] += 1
    return summary
