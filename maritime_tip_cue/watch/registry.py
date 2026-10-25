import argparse
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from maritime_tip_cue import pg
from maritime_tip_cue.config import ROOT, settings
from maritime_tip_cue.watch.pgutil import iso

COLUMNS = "run_key, run_no, folder, origin, attempt_id, created_utc"


def runs_root() -> Path:
    return ROOT / settings()["runs"]["path"]


def folder_name(origin: str, run_no: int, job_id: str | None = None, attempt_id: str | None = None) -> str:
    if origin == "job":
        return f"{job_id[:12]}-{attempt_id[:8]}"
    return f"legacy-run-{run_no}"


def fetch(conn: psycopg.Connection | None, sql: str, params=()) -> list[dict]:
    own = conn is None
    conn = conn or pg.connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        if own:
            conn.rollback()
        return rows
    finally:
        if own:
            conn.close()


def shaped(row: dict) -> dict:
    return row | {"created_utc": iso(row["created_utc"])}


def new_run(origin: str = "legacy", attempt_id: str | None = None, job_id: str | None = None, conn: psycopg.Connection | None = None) -> dict:
    if origin == "job" and not (attempt_id and job_id):
        raise ValueError("a job run needs its job and attempt")
    own = conn is None
    conn = conn or pg.connect()
    try:
        with conn.transaction():
            run_no = conn.execute("select nextval('review.run_no_seq')").fetchone()[0]
            folder = folder_name(origin, run_no, job_id, attempt_id)
            conn.execute(
                "insert into review.runs (run_no, folder, origin, attempt_id, created_utc) values (%s, %s, %s, %s, now())",
                (run_no, folder, origin, attempt_id),
            )
        if own:
            conn.commit()
        return get(run_no, conn)
    finally:
        if own:
            conn.close()


def get(run_no: int, conn: psycopg.Connection | None = None) -> dict:
    rows = fetch(conn, f"select {COLUMNS} from review.runs where run_no = %s", (run_no,))
    if not rows:
        raise KeyError(f"run {run_no} does not exist")
    return shaped(rows[0])


def by_attempt(attempt_id: str, conn: psycopg.Connection | None = None) -> dict | None:
    rows = fetch(conn, f"select {COLUMNS} from review.runs where attempt_id = %s", (attempt_id,))
    return shaped(rows[0]) if rows else None


def listing(conn: psycopg.Connection | None = None) -> list[dict]:
    return [shaped(r) for r in fetch(conn, f"select {COLUMNS} from review.runs order by run_no")]


def folder_of(no: int) -> Path:
    return runs_root() / get(no)["folder"]


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor registry")
    parser.add_argument("command", choices=("list",))
    parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    for r in listing():
        print(f"{r['run_no']:>4}  {r['created_utc']}  {r['origin']:6}  {r['folder']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
