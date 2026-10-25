import csv
import re
import sys
from functools import cache
from pathlib import Path

from maritime_tip_cue.config import ROOT, settings
from maritime_tip_cue.watch.state import Store

MARK = "update review.ais_matches set is_sanctioned = true where run_key = %s and ship_id = %s and chosen"


def list_path() -> Path:
    path = Path(settings()["sanctions"]["path"])
    return path if path.is_absolute() else ROOT / path


def clean_imo(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


@cache
def table(path: str | None = None) -> dict[str, str]:
    target = Path(path) if path else list_path()
    if not target.is_file():
        return {}
    listed: dict[str, set[str]] = {}
    with target.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"imo", "list"}.issubset(reader.fieldnames or []):
            raise ValueError(f"Invalid sanctions CSV: {target}")
        for row in reader:
            imo = clean_imo(row["imo"])
            if imo and row["list"]:
                listed.setdefault(imo, set()).add(row["list"])
    return {imo: "+".join(sorted(lists)) for imo, lists in listed.items()}


def lists_for(imo, listed: dict | None = None) -> str:
    return (table() if listed is None else listed).get(clean_imo(imo), "")


def apply(store: Store, listed: dict | None = None) -> int:
    listed = table() if listed is None else listed
    if not listed:
        return 0
    flagged = 0
    with store.tx():
        for row in store.db.execute("select ship_id, imo from review.vessel_identities where imo is not null and imo <> ''").fetchall():
            lists = lists_for(row["imo"], listed)
            store.db.execute("update review.vessel_identities set sanctions_lists = %s where ship_id = %s", (lists or None, row["ship_id"]))
            if lists:
                store.db.execute(MARK, (store.run_key, row["ship_id"]))
                flagged += 1
        for row in store.db.execute(
            "select distinct m.ship_id, m.imo from review.ais_matches m left join review.vessel_identities v on v.ship_id = m.ship_id "
            "where m.run_key = %s and m.chosen and m.imo is not null and m.imo <> '' and v.ship_id is null",
            (store.run_key,),
        ).fetchall():
            if lists_for(row["imo"], listed):
                store.db.execute(MARK, (store.run_key, row["ship_id"]))
                flagged += 1
    return flagged


def apply_rows(rows: list[dict], listed: dict | None = None) -> int:
    listed = table() if listed is None else listed
    changed = 0
    for row in rows:
        for n in (1, 2):
            lists = lists_for(row.get(f"vessel_{n}_imo"), listed) if listed else ""
            if lists and (not row.get(f"vessel_{n}_is_sanctioned") or row.get(f"vessel_{n}_sanctions_lists") != lists):
                row[f"vessel_{n}_is_sanctioned"] = True
                row[f"vessel_{n}_sanctions_lists"] = lists
                changed += 1
    return changed


def main() -> int:
    listed = table()
    print(f"{len(listed)} listed IMO numbers in {list_path()}", file=sys.stderr)
    return 0 if listed else 1


if __name__ == "__main__":
    sys.exit(main())
