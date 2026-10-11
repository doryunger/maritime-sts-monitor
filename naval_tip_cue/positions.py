import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

SHIP_TYPES = {
    "0": "Unknown",
    "1": "Navigation aid",
    "2": "Fishing",
    "3": "Tug / special craft",
    "4": "High speed craft",
    "6": "Passenger",
    "7": "Cargo",
    "8": "Tanker",
    "9": "Pleasure craft",
}

STALE_MIN = 180
STOPPED_KN = 1.0
SLOW_KN = 3.0
SWING_DEG = 45

FIELDS = [
    "ship_id",
    "name",
    "is_sat",
    "type",
    "type_code",
    "gt_type_code",
    "flag",
    "lat",
    "lon",
    "speed_kn",
    "course",
    "heading",
    "rot",
    "seen_utc",
    "elapsed_min",
    "destination",
    "length_m",
    "width_m",
    "dwt",
    "status",
    "state",
]


def num(value, cast=float, scale: float = 1):
    if value in (None, ""):
        return None
    try:
        return cast(float(value) / scale) if scale != 1 else cast(value)
    except ValueError:
        return None


def estimate_state(speed: float | None, course: int | None, heading: int | None, elapsed: int | None) -> str:
    if elapsed is not None and elapsed > STALE_MIN:
        return "stale"
    if speed is None:
        return "unknown"
    if speed < SLOW_KN and course is not None and heading is not None:
        diff = abs(course - heading) % 360
        if min(diff, 360 - diff) > SWING_DEG:
            return "at anchor (swinging)"
    if speed < STOPPED_KN:
        return "stopped"
    if speed < SLOW_KN:
        return "slow"
    return "underway"


def normalize(row: dict, fetched: datetime) -> dict:
    elapsed = num(row.get("ELAPSED"), int)
    code = row.get("SHIPTYPE")
    speed = num(row.get("SPEED"), scale=10)
    course = num(row.get("COURSE"), int)
    heading = num(row.get("HEADING"), int)
    return {
        "ship_id": row.get("SHIP_ID"),
        "name": row.get("SHIPNAME"),
        "is_sat": row.get("SHIPNAME") == "[SAT-AIS]",
        "type": row.get("TYPE_NAME") or SHIP_TYPES.get(code, code),
        "type_code": code,
        "gt_type_code": row.get("GT_SHIPTYPE"),
        "flag": row.get("FLAG"),
        "lat": num(row.get("LAT")),
        "lon": num(row.get("LON")),
        "speed_kn": speed,
        "course": course,
        "heading": heading,
        "rot": num(row.get("ROT"), int),
        "seen_utc": (fetched - timedelta(minutes=elapsed)).isoformat(timespec="minutes") if elapsed is not None else None,
        "elapsed_min": elapsed,
        "destination": row.get("DESTINATION"),
        "length_m": num(row.get("LENGTH"), int),
        "width_m": num(row.get("WIDTH"), int),
        "dwt": num(row.get("DWT"), int),
        "status": row.get("STATUS_NAME"),
        "state": estimate_state(speed, course, heading, elapsed),
    }


def write(records: list[dict], fmt: str, out) -> None:
    if fmt == "csv":
        w = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(records)
    elif fmt == "jsonl":
        for r in records:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")},
            }
            for r in records
            if r["lat"] is not None and r["lon"] is not None
        ]
        json.dump({"type": "FeatureCollection", "features": features}, out, ensure_ascii=False)
        out.write("\n")


def load(text: str, fetched: datetime | None = None, no_sat: bool = False) -> list[dict]:
    rows = json.loads(text).get("data", {}).get("rows", [])
    fetched = fetched or datetime.now(UTC)
    records = [normalize(r, fetched) for r in rows]
    return [r for r in records if not r["is_sat"]] if no_sat else records


def summary(records: list[dict]) -> str:
    by_type = Counter(r["type"] for r in records)
    by_state = Counter(r["state"] for r in records)
    sat = sum(r["is_sat"] for r in records)
    types = ", ".join(f"{t} {n}" for t, n in by_type.most_common())
    states = ", ".join(f"{t} {n}" for t, n in by_state.most_common())
    return f"{len(records)} vessels ({sat} SAT-AIS): {types}\nstates: {states}"


def run(input: str, fmt: str, out: Path | None, no_sat: bool, fetched: str | None) -> None:
    text = sys.stdin.read() if input == "-" else Path(input).read_text(encoding="utf-8")
    records = load(text, datetime.fromisoformat(fetched) if fetched else None, no_sat)
    print(summary(records), file=sys.stderr)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as f:
            write(records, fmt, f)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        write(records, fmt, sys.stdout)
