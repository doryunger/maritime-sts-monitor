import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from maritime_tip_cue import pg

LEGACY_SCHEMA = """
create table if not exists ais_matches (
    scene_id text, site text, hull_id text, ship_id text, is_sat integer, name text, imo text, mmsi text, flag text,
    seen_utc text, report_age_min real, method text, rep_lat real, rep_lon real, proj_lat real, proj_lon real,
    dist_a_m real, dist_b_m real, limit_m real, ais_length_m real, ais_width_m real, meas_length_m real,
    length_diff_m real, heading_diff_deg real, speed_kn real, chosen integer, slot text, reasons text
);

create table if not exists vessel_identities (
    ship_id text primary key, imo text, mmsi text, call_sign text, gross_tonnage text, dwt text, year_built text,
    vessel_type text, status text, equasis_name text, equasis_flag text, false_flag integer, confidence text,
    reasons text, candidates integer, search_name text, source text, resolved_utc text, sanctions_lists text
);

create table if not exists ais_candidates (
    scene_id text, site text, hull_id text, slot text, ship_id text, is_sat integer, name text,
    type text, type_code text, gt_type_code text, flag text, dwt real, status text, state text,
    seen_utc text, elapsed_min real, captured_utc text, tile_utc text, report_age_min real,
    method text, before_seen_utc text, before_age_min real, after_seen_utc text, after_age_min real,
    bracket_gap_min real, rep_lat real, rep_lon real, proj_lat real, proj_lon real, distance_m real,
    limit_m real, ais_length_m real, ais_width_m real, meas_length_m real, length_diff_m real,
    ais_heading real, hull_heading real, heading_diff_deg real, speed_kn real, course real, rot real,
    decision text, reasons text,
    primary key (scene_id, site, hull_id, slot, ship_id)
);

create table if not exists ais_coverage (
    scene_id text, site text, ships integer, reports integer, window_min real, checked_utc text,
    primary key (scene_id, site)
);
create table if not exists runs (
    run_id integer primary key autoincrement,
    started_utc text, ended_utc text, args text, hot_spot text, window_start text, window_end text,
    sources text, rules_sha1 text, commit_hash text, status text
);
create table if not exists scenes (
    scene_id text, site text, source text, capture_utc text, pass_id text, status text, stage text,
    attempts integer default 0, error text, detector_s real, counts text, data text, folder text,
    updated_utc text, run_id integer,
    primary key (scene_id, site)
);
create table if not exists raw_detections (
    scene_id text, site text, idx integer, col integer, row integer, lat real, lon real, score real,
    feature text,
    primary key (scene_id, site, idx)
);
create table if not exists hulls (
    scene_id text, site text, hull_id text, lat real, lon real, ai2_length_m real, ai2_width_m real,
    ai2_heading real, verdict text, role text, record text,
    primary key (scene_id, site, hull_id)
);
create table if not exists outputs (
    path text primary key, s3_uri text, created_utc text, capture_date text, site text, source text,
    n_rows integer, n_pairs integer, n_possible integer, scene_ids text, sha256 text, uploaded_utc text,
    run_id integer
);
create table if not exists captures (
    finding_id text, format text, local_path text, s3_uri text, run_id integer, created_utc text, uploaded_utc text,
    primary key (finding_id, format)
);
create table if not exists qa_flags (
    hull_id text primary key, scene_id text, source text, verdict text, lat real, lon real, status text,
    correct_verdict text, note text, reviewer text, updated_utc text, imported_utc text
);
create table if not exists events (
    event_id integer primary key autoincrement, time_utc text, run_id integer, level text, scene_id text,
    message text
);
"""

RENAMES = {"run_id": "invocation_id"}
TABLES = (
    ("runs", "run_invocations"),
    ("scenes", "scenes"),
    ("raw_detections", "raw_detections"),
    ("hulls", "hulls"),
    ("outputs", "outputs"),
    ("captures", "captures"),
    ("qa_flags", "qa_flags"),
    ("events", "events"),
    ("ais_coverage", "ais_coverage"),
    ("ais_matches", "ais_matches"),
    ("ais_candidates", "ais_candidates"),
)


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def convert(value, kind: str):
    if value is None:
        return None
    if kind == "timestamp with time zone":
        return None if value == "" else datetime.fromisoformat(value)
    if kind == "boolean":
        return bool(value)
    if kind == "jsonb":
        return json.dumps(clean(json.loads(value)), allow_nan=False)
    if kind in ("integer", "bigint"):
        return int(value)
    if kind == "double precision":
        return float(value)
    return value


def origin_of(folder: str) -> str:
    return "legacy" if folder.startswith("legacy-") else "job"


def snapshot(path: Path) -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    memory = sqlite3.connect(":memory:")
    source.backup(memory)
    source.close()
    return memory


def registry_rows(root: Path) -> list[tuple[int, str, str]]:
    db = sqlite3.connect(f"file:{(root / 'registry.db').as_posix()}?mode=ro", uri=True)
    try:
        return db.execute("select run_no, folder, created_utc from runs order by run_no").fetchall()
    finally:
        db.close()


def column_types(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute("select column_name, data_type from information_schema.columns where table_schema = 'review' and table_name = %s", (table,))
    return {name: kind for name, kind in rows}


def copy_table(conn: psycopg.Connection, source: sqlite3.Connection, sqlite_table: str, pg_table: str, run_key: int) -> int:
    have = {r[0] for r in source.execute("select name from sqlite_master where type = 'table'")}
    if sqlite_table not in have:
        return 0
    types = column_types(conn, pg_table)
    names = [r[1] for r in source.execute(f"pragma table_info({sqlite_table})")]
    targets = [RENAMES.get(n, n) for n in names]
    unmapped = [t for t in targets if t not in types]
    if unmapped:
        raise RuntimeError(f"{sqlite_table}: columns without a Postgres column: {', '.join(unmapped)}")
    n = 0
    columns = ", ".join(["run_key"] + targets)
    with conn.cursor() as cur, cur.copy(f"copy review.{pg_table} ({columns}) from stdin") as out:
        for row in source.execute(f"select {', '.join(names)} from {sqlite_table}"):
            out.write_row([run_key] + [convert(v, types[t]) for v, t in zip(row, targets)])
            n += 1
    return n


def source_counts(source: sqlite3.Connection) -> dict[str, int]:
    have = {r[0] for r in source.execute("select name from sqlite_master where type = 'table'")}
    return {pg_table: (source.execute(f"select count(*) from {t}").fetchone()[0] if t in have else 0) for t, pg_table in TABLES}


def import_run(conn: psycopg.Connection, root: Path, run_no: int, folder: str, created: str) -> dict[str, int]:
    source = snapshot(root / folder / "state.db")
    expected = source_counts(source)
    with conn.transaction():
        run_key = conn.execute(
            "insert into review.runs (folder, run_no, origin, created_utc) values (%s, %s, %s, %s) returning run_key",
            (folder, run_no, origin_of(folder), datetime.fromisoformat(created) if created else None),
        ).fetchone()[0]
        got = {}
        for sqlite_table, pg_table in TABLES:
            got[pg_table] = copy_table(conn, source, sqlite_table, pg_table, run_key)
        if got != expected:
            raise RuntimeError(f"{folder}: imported {got} but the source holds {expected}")
        conn.execute("update review.runs set import_counts = %s where run_key = %s", (json.dumps(got), run_key))
        conn.execute(pg.LINKS)
        merge_identities(conn, source)
    source.close()
    return got


def merge_identities(conn: psycopg.Connection, source: sqlite3.Connection) -> int:
    have = {r[0] for r in source.execute("select name from sqlite_master where type = 'table'")}
    if "vessel_identities" not in have:
        return 0
    types = column_types(conn, "vessel_identities")
    names = [r[1] for r in source.execute("pragma table_info(vessel_identities)")]
    unmapped = [n for n in names if n not in types]
    if unmapped:
        raise RuntimeError(f"vessel_identities: columns without a Postgres column: {', '.join(unmapped)}")
    sql = (
        f"insert into review.vessel_identities ({', '.join(names)}) values ({', '.join(['%s'] * len(names))}) "
        "on conflict (ship_id) do update set "
        + ", ".join(f"{n} = excluded.{n}" for n in names if n != "ship_id")
        + " where excluded.resolved_utc > review.vessel_identities.resolved_utc"
    )
    n = 0
    for row in source.execute(f"select {', '.join(names)} from vessel_identities"):
        conn.execute(sql, [convert(v, types[c]) for v, c in zip(row, names)])
        n += 1
    return n


def run_import(root: Path, only: list[int], replace: bool) -> int:
    with pg.connect() as conn:
        pg.init(conn)
        known = {folder: key for folder, key in conn.execute("select folder, run_key from review.runs")}
        for run_no, folder, created in registry_rows(root):
            if only and run_no not in only:
                continue
            if folder in known:
                if not replace:
                    print(f"run {run_no:3} {folder}: already imported, skipped")
                    continue
                conn.execute("delete from review.runs where run_key = %s", (known[folder],))
                conn.commit()
            if not (root / folder / "state.db").exists():
                print(f"run {run_no:3} {folder}: no state.db, skipped")
                continue
            got = import_run(conn, root, run_no, folder, created)
            conn.commit()
            print(f"run {run_no:3} {folder}: " + ", ".join(f"{k} {v}" for k, v in got.items() if v))
        pg.init(conn)
    return 0


def flag_digest(rows) -> str:
    return hashlib.sha256(json.dumps(sorted(rows), default=str).encode()).hexdigest()


def verify(root: Path) -> int:
    bad = 0
    with pg.connect() as conn:
        keys = {folder: key for folder, key in conn.execute("select folder, run_key from review.runs")}
        for run_no, folder, _ in registry_rows(root):
            if folder not in keys:
                print(f"run {run_no:3} {folder}: NOT IMPORTED")
                bad += 1
                continue
            key = keys[folder]
            source = snapshot(root / folder / "state.db")
            problems = []
            stored = dict(conn.execute("select import_counts from review.runs where run_key = %s", (key,)).fetchone()[0])
            for t, n in source_counts(source).items():
                live = conn.execute(f"select count(*) from review.{t} where run_key = %s", (key,)).fetchone()[0]
                if live != n or stored.get(t) != n:
                    problems.append(f"{t}: source {n}, postgres {live}")
            local = flag_digest((r[0], r[1], r[2], r[3], r[4]) for r in source.execute("select hull_id, status, correct_verdict, note, reviewer from qa_flags"))
            remote = flag_digest((r[0], r[1], r[2], r[3], r[4]) for r in conn.execute("select hull_id, status, correct_verdict, note, reviewer from review.qa_flags where run_key = %s", (key,)))
            if local != remote:
                problems.append("review flags differ")
            if source.execute("select 1 from sqlite_master where name = 'findings'").fetchone():
                a = dict(source.execute("select verdict, count(*) from findings group by verdict").fetchall())
                b = dict(conn.execute("select verdict, count(*) from review.findings where run_key = %s group by verdict", (key,)).fetchall())
                if a != b:
                    problems.append(f"findings differ: source {a}, postgres {b}")
            source.close()
            if problems:
                bad += 1
                print(f"run {run_no:3} {folder}: " + "; ".join(problems))
        total = pg.counts(conn)
    print("rows in postgres:", ", ".join(f"{t} {n}" for t, n in total.items()))
    print("verify: " + ("all runs match" if not bad else f"{bad} run(s) differ"))
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor pgimport")
    parser.add_argument("command", choices=("import", "verify"))
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--run", type=int, action="append", default=[])
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(args.runs_root)
    if args.command == "verify":
        return verify(root)
    return run_import(root, args.run, args.replace)


if __name__ == "__main__":
    sys.exit(main())
