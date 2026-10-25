import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from maritime_tip_cue import pg, pgimport
from maritime_tip_cue.catalogue import canonical_bytes, digest

TABLES = (
    ("scans", ("scan_id",)),
    ("channels", ("site", "source")),
    ("jobs", ("job_id",)),
    ("job_attempts", ("attempt_id",)),
    ("observations", ("site", "source", "scene", "version")),
    ("scan_sources", ("scan_id", "site", "source")),
)
RUN_FOLDER = re.compile(r"([0-9a-f]{12})-([0-9a-f]{8})")
EXAMPLES = 5


class Rollback(Exception):
    pass


def column_types(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = 'processing' and table_name = %s",
        (table,),
    )
    return {name: kind for name, kind in rows}


def value_of(value, kind: str):
    converted = pgimport.convert(value, kind)
    if kind == "jsonb" and converted is not None:
        return Jsonb(json.loads(converted))
    return converted


def source_names(source: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in source.execute(f"pragma table_info({table})")]


def source_rows(source: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple]]:
    have = {r[0] for r in source.execute("select name from sqlite_master where type = 'table'")}
    if table not in have:
        return [], []
    names = source_names(source, table)
    return names, source.execute(f"select {', '.join(names)} from {table}").fetchall()


def copy_table(conn: psycopg.Connection, source: sqlite3.Connection, table: str, keys: tuple[str, ...]) -> dict:
    types = column_types(conn, table)
    names, rows = source_rows(source, table)
    unmapped = [n for n in names if n not in types]
    if unmapped:
        raise RuntimeError(f"{table}: columns without a Postgres column: {', '.join(unmapped)}")
    sql = (
        f"insert into processing.{table} ({', '.join(names)}) values ({', '.join(['%s'] * len(names))}) "
        f"on conflict ({', '.join(keys)}) do nothing"
    )
    inserted = 0
    for row in rows:
        inserted += conn.execute(sql, [value_of(v, types[n]) for v, n in zip(row, names)]).rowcount
    position = [names.index(k) for k in keys]
    wanted = {tuple(row[i] for i in position) for row in rows}
    present = set(conn.execute(f"select {', '.join(keys)} from processing.{table}").fetchall())
    missing = wanted - present
    report = {"source": len(rows), "inserted": inserted, "skipped": len(rows) - inserted, "missing": len(missing)}
    if missing or len(wanted) != len(rows):
        raise RuntimeError(
            f"{table}: {len(missing)} source rows absent after the import, {len(rows) - len(wanted)} duplicate keys"
        )
    return report


def assign_scans(scans: list[tuple[str, datetime, datetime | None]], moments: dict) -> dict:
    ordered = sorted(scans, key=lambda s: s[1])
    result = {"assigned": {}, "contained": 0, "preceding": 0, "ambiguous": [], "unmatched": []}
    for key, moment in moments.items():
        inside = [s for s in ordered if s[1] <= moment and s[2] is not None and moment <= s[2]]
        if len(inside) == 1:
            result["assigned"][key] = inside[0][0]
            result["contained"] += 1
            continue
        if len(inside) > 1:
            result["ambiguous"].append(key)
            continue
        before = [s for s in ordered if s[1] <= moment]
        if before:
            last = before[-1]
            if last[2] is None or moment - last[2] < last[2] - last[1]:
                result["assigned"][key] = last[0]
                result["preceding"] += 1
                continue
        result["unmatched"].append(key)
    return result


def summarize(found: dict) -> dict:
    return {
        "contained": found["contained"],
        "preceding": found["preceding"],
        "ambiguous": len(found["ambiguous"]),
        "unmatched": len(found["unmatched"]),
        "ambiguous_examples": [list(k) if isinstance(k, tuple) else k for k in found["ambiguous"][:EXAMPLES]],
        "unmatched_examples": [list(k) if isinstance(k, tuple) else k for k in found["unmatched"][:EXAMPLES]],
    }


def derive_scan_ids(conn: psycopg.Connection) -> dict:
    scans = conn.execute("select scan_id, started_utc, ended_utc from processing.scans").fetchall()
    observations = {
        (r[0], r[1], r[2], r[3]): r[4]
        for r in conn.execute(
            "select site, source, scene, version, first_discovered_utc from processing.observations "
            "where scan_id is null"
        )
    }
    jobs = {r[0]: r[1] for r in conn.execute("select job_id, created_utc from processing.jobs where scan_id is null")}
    report = {}
    for label, moments, sql, arity in (
        (
            "observations",
            observations,
            "update processing.observations set scan_id = %s "
            "where site = %s and source = %s and scene = %s and version = %s",
            4,
        ),
        ("jobs", jobs, "update processing.jobs set scan_id = %s where job_id = %s", 1),
    ):
        found = assign_scans(scans, moments)
        for key, scan_id in found["assigned"].items():
            conn.execute(sql, (scan_id, *(key if arity > 1 else (key,))))
        report[label] = summarize(found)
    report["scan_sources_new_counts"] = new_count_mismatches(conn)
    return report


def new_count_mismatches(conn: psycopg.Connection) -> list:
    rows = conn.execute(
        "select s.scan_id, s.site, s.source, s.new, count(o.scene) from processing.scan_sources s "
        "left join processing.observations o on o.scan_id = s.scan_id and o.site = s.site and o.source = s.source "
        "group by s.scan_id, s.site, s.source, s.new having s.new <> count(o.scene)"
    ).fetchall()
    return [list(r) for r in rows[:EXAMPLES]] + ([f"{len(rows)} in total"] if len(rows) > EXAMPLES else [])


def job_scene_gaps(conn: psycopg.Connection) -> list[str]:
    bad = []
    for job_id, scenes in conn.execute("select job_id, manifest_json->'scenes' from processing.jobs").fetchall():
        wanted = {(s["site"], s["source"], s["scene"]) for s in scenes}
        have = {
            tuple(r)
            for r in conn.execute(
                "select site, source, scene from processing.job_scenes where job_id = %s", (job_id,)
            )
        }
        if have != wanted:
            bad.append(job_id)
    return bad


def derive_job_scenes(conn: psycopg.Connection) -> dict:
    inserted, jobs = 0, 0
    for job_id, scenes in conn.execute("select job_id, manifest_json->'scenes' from processing.jobs").fetchall():
        jobs += 1
        for site, source, scene in sorted({(s["site"], s["source"], s["scene"]) for s in scenes}):
            inserted += conn.execute(
                "insert into processing.job_scenes (job_id, site, source, scene) values (%s, %s, %s, %s) "
                "on conflict do nothing",
                (job_id, site, source, scene),
            ).rowcount
    gaps = job_scene_gaps(conn)
    return {"jobs": jobs, "inserted": inserted, "mismatched": len(gaps), "mismatched_jobs": gaps[:EXAMPLES]}


def link_runs(conn: psycopg.Connection) -> dict:
    report = {"linked": 0, "no_match": [], "several": [], "unparsable": [], "legacy_left_null": 0}
    report["legacy_left_null"] = conn.execute("select count(*) from review.runs where origin = 'legacy'").fetchone()[0]
    for run_key, folder in conn.execute(
        "select run_key, folder from review.runs where origin <> 'legacy' and attempt_id is null order by run_key"
    ).fetchall():
        match = RUN_FOLDER.fullmatch(folder)
        if not match:
            report["unparsable"].append(folder)
            continue
        found = conn.execute(
            "select attempt_id from processing.job_attempts where left(job_id, 12) = %s and left(attempt_id, 8) = %s",
            match.groups(),
        ).fetchall()
        if len(found) == 1:
            conn.execute("update review.runs set attempt_id = %s where run_key = %s", (found[0][0], run_key))
            report["linked"] += 1
        elif found:
            report["several"].append(folder)
        else:
            report["no_match"].append(folder)
    return {k: (v[:EXAMPLES] if isinstance(v, list) else v) for k, v in report.items()} | {
        "no_match_count": len(report["no_match"]),
        "several_count": len(report["several"]),
        "unparsable_count": len(report["unparsable"]),
    }


def manifest_problems(conn: psycopg.Connection) -> list[str]:
    problems = []
    for job_id, manifest, sha in conn.execute("select job_id, manifest_json, manifest_sha256 from processing.jobs"):
        core = {key: manifest[key] for key in ("schema_version", "site", "scenes", "rules", "rules_sha256")}
        if digest(core) != job_id or manifest.get("job_id") != job_id or sha != job_id:
            problems.append(job_id)
    return problems


def run_import(path: Path, dry_run: bool) -> int:
    source = pgimport.snapshot(path)
    report = {"tables": {}}
    conn = pg.connect()
    conn.commit()
    conn.autocommit = True
    try:
        try:
            with conn.transaction():
                for table, keys in TABLES:
                    report["tables"][table] = copy_table(conn, source, table, keys)
                report["scan_ids"] = derive_scan_ids(conn)
                report["job_scenes"] = derive_job_scenes(conn)
                report["runs"] = link_runs(conn)
                report["manifest_digest_mismatches"] = len(manifest_problems(conn))
                if report["manifest_digest_mismatches"]:
                    raise RuntimeError("stored manifests no longer match their job ids")
                if dry_run:
                    raise Rollback()
        except Rollback:
            report["dry_run"] = "rolled back"
    finally:
        conn.close()
        source.close()
    print(json.dumps(report, sort_keys=True, indent=1))
    return 0


def run_verify(path: Path) -> int:
    source = pgimport.snapshot(path)
    problems = {}
    with pg.connect() as conn:
        tables = {}
        for table, keys in TABLES:
            names, rows = source_rows(source, table)
            position = [names.index(k) for k in keys]
            wanted = {tuple(row[i] for i in position) for row in rows}
            present = set(conn.execute(f"select {', '.join(keys)} from processing.{table}").fetchall())
            total = conn.execute(f"select count(*) from processing.{table}").fetchone()[0]
            tables[table] = {"source": len(rows), "postgres": total, "missing": len(wanted - present)}
        manifests = manifest_problems(conn)
        stored = {
            r[0]: canonical_bytes(r[1]) for r in conn.execute("select job_id, manifest_json from processing.jobs")
        }
        names, rows = source_rows(source, "jobs")
        index = names.index("manifest_json")
        id_at = names.index("job_id")
        differing = [row[id_at] for row in rows if stored.get(row[id_at]) != canonical_bytes(json.loads(row[index]))]
        scenes = job_scene_gaps(conn)
        unresolved = {
            label: conn.execute(sql).fetchone()[0]
            for label, sql in {
                "observations_without_scan": "select count(*) from processing.observations where scan_id is null",
                "jobs_without_scan": "select count(*) from processing.jobs where scan_id is null",
                "job_runs_without_attempt": "select count(*) from review.runs "
                "where origin = 'job' and attempt_id is null",
                "legacy_runs": "select count(*) from review.runs where origin = 'legacy'",
            }.items()
        }
        if any(t["missing"] for t in tables.values()):
            problems["rows_missing"] = {t: v["missing"] for t, v in tables.items() if v["missing"]}
        if manifests:
            problems["manifest_digest"] = manifests[:EXAMPLES]
        if differing:
            problems["manifest_differs_from_source"] = differing[:EXAMPLES]
        if scenes:
            problems["job_scenes_incomplete"] = scenes[:EXAMPLES]
    source.close()
    print(json.dumps({"tables": tables, "unresolved": unresolved, "problems": problems}, sort_keys=True, indent=1))
    return 1 if problems else 0


def adoption_scan_id(moment: datetime) -> str:
    return "adopted-" + moment.strftime("%Y%m%dT%H%M%SZ")


def run_adopt(job_ids: list[str], note: str) -> int:
    with pg.connect() as conn:
        found = {
            r[0]: r
            for r in conn.execute(
                "select job_id, created_utc, scan_id from processing.jobs where job_id = any(%s)", (job_ids,)
            )
        }
        unknown = [j for j in job_ids if j not in found]
        if unknown:
            print(json.dumps({"refused": {"unknown_jobs": unknown}}, indent=1))
            return 1
        scan_id = adoption_scan_id(min(r[1] for r in found.values()))
        placed = sorted(j for j, r in found.items() if r[2] not in (None, scan_id))
        if placed:
            print(json.dumps({"refused": {"jobs_that_already_have_a_scan": placed}}, indent=1))
            return 1
        moment = min(r[1] for r in found.values())
        summary = {"kind": "manual adoption", "note": note, "jobs": sorted(job_ids)}
        with conn.transaction():
            conn.execute(
                "insert into processing.scans (scan_id, started_utc, ended_utc, status, summary_json) "
                "values (%s, %s, %s, 'adopted', %s) on conflict (scan_id) do nothing",
                (scan_id, moment, moment, Jsonb(summary)),
            )
            changed = conn.execute(
                "update processing.jobs set scan_id = %s where job_id = any(%s) and scan_id is null", (scan_id, job_ids)
            ).rowcount
    print(json.dumps({"scan_id": scan_id, "jobs_linked": changed, "jobs": sorted(job_ids)}, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor pgledger")
    parser.add_argument("command", choices=("import", "verify", "adopt"))
    parser.add_argument("--tracker-db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--job", action="append", default=[])
    parser.add_argument("--note")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "adopt":
        if not args.job or not args.note:
            parser.error("adopt needs at least one --job and a --note")
        return run_adopt(args.job, args.note)
    if not args.tracker_db:
        parser.error(f"{args.command} needs --tracker-db")
    path = Path(args.tracker_db)
    if args.command == "verify":
        return run_verify(path)
    return run_import(path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
