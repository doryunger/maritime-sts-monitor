import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from maritime_tip_cue import pg
from maritime_tip_cue.equasis.client import cache_path

SOURCE = "aggregator"


def text(value) -> str | None:
    value = (value or "").strip()
    return value or None


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    out = []
    for row in rows:
        if not text(row.get("ship_id")) or not text(row.get("imo")):
            continue
        out.append(
            {
                "ship_id": row["ship_id"].strip(),
                "imo": text(row["imo"]),
                "mmsi": text(row.get("mmsi")),
                "call_sign": text(row.get("call_sign")),
                "gross_tonnage": text(row.get("gross_tonnage")),
                "dwt": text(row.get("equasis_dwt")) or text(row.get("dwt")),
                "year_built": text(row.get("year_built")),
                "vessel_type": text(row.get("equasis_type")),
                "status": text(row.get("status")),
                "equasis_name": text(row.get("equasis_name")),
                "equasis_flag": text(row.get("equasis_flag")),
                "false_flag": str(row.get("false_flag")).strip().lower() == "true",
                "confidence": text(row.get("confidence")),
                "reasons": text(row.get("reasons")),
                "candidates": int(row["candidates"]) if text(row.get("candidates")) else None,
                "search_name": None,
                "source": SOURCE,
                "resolved_utc": datetime.fromisoformat(row["resolved_utc"]) if text(row.get("resolved_utc")) else None,
            }
        )
    return out


def seed(conn, rows: list[dict]) -> dict:
    columns = list(rows[0]) if rows else []
    inserted = 0
    for row in rows:
        inserted += conn.execute(
            f"insert into review.vessel_identities ({', '.join(columns)}) values ({', '.join(['%s'] * len(columns))}) "
            "on conflict (ship_id) do nothing",
            [row[c] for c in columns],
        ).rowcount
    return {"resolved_in_file": len(rows), "inserted": inserted, "already_known": len(rows) - inserted}


def merge_cache(source: Path, target: Path) -> dict:
    incoming = json.loads(source.read_text(encoding="utf-8"))
    current = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"search": {}, "details": {}}
    added = {}
    for part in ("search", "details"):
        current.setdefault(part, {})
        new = {k: v for k, v in incoming.get(part, {}).items() if k not in current[part]}
        current[part].update(new)
        added[part] = len(new)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, indent=1, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    return added | {"total_search": len(current["search"]), "total_details": len(current["details"])}


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor identity_seed")
    parser.add_argument("--vessel-ids", required=True)
    parser.add_argument("--cache")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rows = read_rows(Path(args.vessel_ids))
    with pg.connect() as conn:
        result = seed(conn, rows)
        if args.dry_run:
            conn.rollback()
    print(json.dumps({"identities": result, "dry_run": args.dry_run}, sort_keys=True))
    if args.cache and not args.dry_run:
        print(json.dumps({"cache": merge_cache(Path(args.cache), cache_path())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
