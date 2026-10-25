import argparse
import sys

import psycopg

from maritime_tip_cue.config import env

SCHEMA = """
create schema if not exists review;

create table if not exists review.runs (
    run_key bigserial primary key,
    folder text not null unique,
    run_no integer,
    origin text not null,
    created_utc timestamptz,
    imported_utc timestamptz not null default now(),
    import_counts jsonb
);

create sequence if not exists review.run_no_seq;
alter table review.runs alter column run_no set default nextval('review.run_no_seq');
alter table review.runs add column if not exists attempt_id text;
alter table review.runs drop column if exists job_id;
alter table review.runs drop column if exists review_prefix;
create unique index if not exists runs_by_run_no on review.runs (run_no);
select setval(
    'review.run_no_seq',
    greatest((select coalesce(max(run_no), 0) from review.runs), (select case when is_called then last_value else 0 end from review.run_no_seq), 1),
    greatest((select coalesce(max(run_no), 0) from review.runs), (select case when is_called then last_value else 0 end from review.run_no_seq)) > 0
);

create table if not exists review.run_invocations (
    run_key bigint not null references review.runs on delete cascade,
    invocation_id integer not null,
    started_utc timestamptz, ended_utc timestamptz, args jsonb, hot_spot text, window_start text, window_end text,
    sources text, rules_sha1 text, commit_hash text, status text,
    primary key (run_key, invocation_id)
);

create table if not exists review.scenes (
    run_key bigint not null references review.runs on delete cascade,
    scene_id text not null, site text not null, source text, capture_utc timestamptz, pass_id text, status text,
    stage text, attempts integer default 0, error text, detector_s double precision, counts jsonb, data jsonb,
    folder text, updated_utc timestamptz, invocation_id integer,
    primary key (run_key, scene_id, site)
);

create table if not exists review.raw_detections (
    run_key bigint not null references review.runs on delete cascade,
    scene_id text not null, site text not null, idx integer not null, col integer, row integer,
    lat double precision, lon double precision, score double precision, feature jsonb,
    primary key (run_key, scene_id, site, idx)
);

create table if not exists review.hulls (
    run_key bigint not null references review.runs on delete cascade,
    scene_id text not null, site text not null, hull_id text not null, lat double precision, lon double precision,
    ai2_length_m double precision, ai2_width_m double precision, ai2_heading double precision, verdict text,
    role text, record jsonb,
    primary key (run_key, scene_id, site, hull_id)
);
create index if not exists hulls_by_hull on review.hulls (scene_id, hull_id);

create table if not exists review.outputs (
    run_key bigint not null references review.runs on delete cascade,
    path text not null, s3_uri text, created_utc timestamptz, capture_date text, site text, source text,
    n_rows integer, n_pairs integer, n_possible integer, scene_ids text, sha256 text, uploaded_utc timestamptz,
    invocation_id integer,
    primary key (run_key, path)
);

create table if not exists review.captures (
    run_key bigint not null references review.runs on delete cascade,
    finding_id text not null, format text not null, local_path text, s3_uri text, invocation_id integer,
    created_utc timestamptz, uploaded_utc timestamptz,
    primary key (run_key, finding_id, format)
);

create table if not exists review.qa_flags (
    run_key bigint not null references review.runs on delete cascade,
    hull_id text not null, scene_id text, source text, verdict text, lat double precision, lon double precision,
    status text, correct_verdict text, note text, reviewer text, updated_utc timestamptz, imported_utc timestamptz,
    primary key (run_key, hull_id)
);
create index if not exists qa_flags_by_hull on review.qa_flags (hull_id);

create table if not exists review.events (
    run_key bigint not null references review.runs on delete cascade,
    event_id integer not null, time_utc timestamptz, invocation_id integer, level text, scene_id text, message text,
    primary key (run_key, event_id)
);

create table if not exists review.ais_coverage (
    run_key bigint not null references review.runs on delete cascade,
    scene_id text not null, site text not null, ships integer, reports integer, window_min double precision,
    checked_utc timestamptz,
    primary key (run_key, scene_id, site)
);

create table if not exists review.ais_matches (
    id bigserial primary key,
    run_key bigint not null references review.runs on delete cascade,
    scene_id text, site text, hull_id text, ship_id text, is_sat boolean, name text, imo text, mmsi text, flag text,
    seen_utc timestamptz, report_age_min double precision, method text, rep_lat double precision,
    rep_lon double precision, proj_lat double precision, proj_lon double precision, dist_a_m double precision,
    dist_b_m double precision, limit_m double precision, ais_length_m double precision,
    ais_width_m double precision, meas_length_m double precision, length_diff_m double precision,
    heading_diff_deg double precision, speed_kn double precision, chosen boolean, slot text, reasons text,
    is_sanctioned boolean not null default false
);
create index if not exists ais_matches_by_run on review.ais_matches (run_key, scene_id, hull_id);

create table if not exists review.ais_candidates (
    run_key bigint not null references review.runs on delete cascade,
    scene_id text not null, site text not null, hull_id text not null, slot text not null, ship_id text not null,
    is_sat boolean, name text, type text, type_code text, gt_type_code text, flag text, dwt double precision,
    status text, state text, seen_utc timestamptz, elapsed_min double precision, captured_utc timestamptz,
    tile_utc timestamptz, report_age_min double precision, method text, before_seen_utc timestamptz,
    before_age_min double precision, after_seen_utc timestamptz, after_age_min double precision,
    bracket_gap_min double precision, rep_lat double precision, rep_lon double precision,
    proj_lat double precision, proj_lon double precision, distance_m double precision, limit_m double precision,
    ais_length_m double precision, ais_width_m double precision, meas_length_m double precision,
    length_diff_m double precision, ais_heading double precision, hull_heading double precision,
    heading_diff_deg double precision, speed_kn double precision, course double precision, rot double precision,
    decision text, reasons text, is_sanctioned boolean not null default false,
    primary key (run_key, scene_id, site, hull_id, slot, ship_id)
);

create table if not exists review.vessel_identities (
    ship_id text primary key, imo text, mmsi text, call_sign text, gross_tonnage text, dwt text, year_built text,
    vessel_type text, status text, equasis_name text, equasis_flag text, false_flag boolean, confidence text,
    reasons text, candidates integer, search_name text, source text, resolved_utc timestamptz, sanctions_lists text
);

create schema if not exists processing;

create table if not exists processing.scans (
    scan_id text primary key, started_utc timestamptz not null, ended_utc timestamptz, status text not null,
    summary_json jsonb
);

create table if not exists processing.channels (
    site text not null, source text not null, baseline_utc timestamptz not null,
    primary key (site, source)
);

create table if not exists processing.observations (
    site text not null, source text not null, scene text not null, version text not null,
    processing_group text not null, first_discovered_utc timestamptz not null, last_seen_utc timestamptz not null,
    row_json jsonb not null,
    primary key (site, source, scene, version)
);

create table if not exists processing.jobs (
    job_id text primary key, site text not null, source text not null, created_utc timestamptz not null,
    manifest_json jsonb not null, manifest_sha256 text not null, status text not null, dispatched_utc timestamptz,
    completed_utc timestamptz, error text, receipt_json jsonb, retry_count integer not null default 0,
    available_utc timestamptz, current_attempt_id text, superseded_by text
);
create index if not exists jobs_by_status on processing.jobs (status);

create table if not exists processing.job_attempts (
    attempt_id text primary key, job_id text not null, attempt_no integer not null, owner text not null,
    status text not null, started_utc timestamptz not null, finished_utc timestamptz, error text,
    receipt_json jsonb
);
create index if not exists attempts_by_job on processing.job_attempts (job_id);

create table if not exists processing.scan_sources (
    scan_id text not null, site text not null, source text not null, status text not null, matched integer not null,
    new integer not null, baseline integer not null, error text,
    primary key (scan_id, site, source)
);

create or replace view review.findings as
select h.run_key, h.scene_id, h.site, h.hull_id, s.source, s.capture_utc, h.lat, h.lon, h.role,
    case when h.verdict = 'moving' then 'single' else h.verdict end as run_verdict,
    f.status as qa_status, f.correct_verdict as qa_correct_verdict, f.note as qa_note, f.updated_utc as qa_updated_utc,
    case
        when f.status = 'wrong_verdict' then case when f.correct_verdict = 'moving' then 'single' else coalesce(f.correct_verdict, 'unresolved') end
        when f.status in ('is_single', 'is_moving') then 'single'
        when f.status = 'is_pair' then 'pair'
        when f.status in ('bad_geometry', 'other') then 'excluded'
        when f.status = 'ok' then case when f.verdict = 'moving' then 'single' else f.verdict end
        else case when h.verdict = 'moving' then 'single' else h.verdict end
    end as verdict
from review.hulls h
join review.scenes s on s.run_key = h.run_key and s.scene_id = h.scene_id and s.site = h.site
left join review.qa_flags f on f.run_key = h.run_key and f.hull_id = h.scene_id || '__' || h.hull_id
where coalesce(h.role, '') not in ('duplicate', 'hull_b_box');

alter table review.qa_flags add column if not exists site text;
alter table review.qa_flags add column if not exists hull text;
alter table review.captures add column if not exists scene_id text;
alter table review.captures add column if not exists site text;
alter table review.captures add column if not exists hull text;
alter table review.vessel_identities add column if not exists first_run_key bigint;
alter table processing.observations add column if not exists scan_id text;
alter table processing.jobs add column if not exists scan_id text;

create table if not exists review.output_scenes (
    run_key bigint not null, path text not null, scene_id text not null,
    primary key (run_key, path, scene_id),
    foreign key (run_key, path) references review.outputs (run_key, path) on delete cascade
);

create table if not exists processing.job_scenes (
    job_id text not null references processing.jobs on delete cascade,
    site text not null, source text not null, scene text not null,
    primary key (job_id, site, source, scene)
);
create index if not exists job_scenes_by_scene on processing.job_scenes (site, source, scene);

create or replace view review.run_lineage as
select r.run_key, r.run_no, r.origin, r.folder, r.attempt_id, a.attempt_no, a.status as attempt_status,
    j.job_id, j.status as job_status, j.scan_id, j.site, j.source
from review.runs r
left join processing.job_attempts a on a.attempt_id = r.attempt_id
left join processing.jobs j on j.job_id = a.job_id;

do $$
declare c record;
begin
    for c in select * from (values
        ('hulls_scene_fk', 'review.hulls', 'foreign key (run_key, scene_id, site) references review.scenes (run_key, scene_id, site) on delete cascade'),
        ('raw_detections_scene_fk', 'review.raw_detections', 'foreign key (run_key, scene_id, site) references review.scenes (run_key, scene_id, site) on delete cascade'),
        ('ais_coverage_scene_fk', 'review.ais_coverage', 'foreign key (run_key, scene_id, site) references review.scenes (run_key, scene_id, site) on delete cascade'),
        ('ais_matches_hull_fk', 'review.ais_matches', 'foreign key (run_key, scene_id, site, hull_id) references review.hulls (run_key, scene_id, site, hull_id) on delete cascade'),
        ('ais_candidates_hull_fk', 'review.ais_candidates', 'foreign key (run_key, scene_id, site, hull_id) references review.hulls (run_key, scene_id, site, hull_id) on delete cascade'),
        ('qa_flags_hull_fk', 'review.qa_flags', 'foreign key (run_key, scene_id, site, hull) references review.hulls (run_key, scene_id, site, hull_id) on delete cascade'),
        ('captures_hull_fk', 'review.captures', 'foreign key (run_key, scene_id, site, hull) references review.hulls (run_key, scene_id, site, hull_id) on delete cascade'),
        ('vessel_identities_run_fk', 'review.vessel_identities', 'foreign key (first_run_key) references review.runs (run_key) on delete set null'),
        ('job_attempts_job_fk', 'processing.job_attempts', 'foreign key (job_id) references processing.jobs (job_id)'),
        ('observations_scan_fk', 'processing.observations', 'foreign key (scan_id) references processing.scans (scan_id)'),
        ('jobs_scan_fk', 'processing.jobs', 'foreign key (scan_id) references processing.scans (scan_id)'),
        ('scan_sources_scan_fk', 'processing.scan_sources', 'foreign key (scan_id) references processing.scans (scan_id)'),
        ('runs_attempt_fk', 'review.runs', 'foreign key (attempt_id) references processing.job_attempts (attempt_id)')
    ) as t(name, tbl, ddl)
    loop
        if not exists (select 1 from pg_constraint where conname = c.name) then
            execute format('alter table %s add constraint %I %s', c.tbl, c.name, c.ddl);
        end if;
    end loop;
end $$;
"""

LINKS = """
update review.qa_flags f set site = h.site, hull = h.hull_id
from review.hulls h
where f.site is null and h.run_key = f.run_key and h.scene_id = f.scene_id
    and (f.hull_id = h.scene_id || '__' || h.hull_id or f.hull_id = h.scene_id || '__' || h.hull_id || '__' || h.site);

insert into review.output_scenes (run_key, path, scene_id)
select o.run_key, o.path, trim(s) from review.outputs o, unnest(string_to_array(o.scene_ids, ',')) as s
where o.scene_ids is not null and trim(s) <> ''
on conflict do nothing;

update review.captures c set scene_id = m.scene_id, site = m.site, hull = m.hull_id
from (
    select cap.run_key, cap.finding_id, min(h.scene_id) as scene_id, min(h.site) as site, min(h.hull_id) as hull_id
    from (
        select distinct run_key, finding_id, split_part(finding_id, '_', 1) as src, split_part(finding_id, '_', 2) as ts,
            regexp_replace(finding_id, '^[^_]+_[^_]+_', '') as core
        from review.captures where hull is null
    ) cap
    join review.scenes s on s.run_key = cap.run_key and s.source = cap.src
        and to_char(s.capture_utc, 'YYYYMMDD"T"HH24MISS') = cap.ts
    join review.hulls h on h.run_key = s.run_key and h.scene_id = s.scene_id and h.site = s.site
        and regexp_replace(replace(h.hull_id, '_', '-'), '-+', '-', 'g') = cap.core
    group by cap.run_key, cap.finding_id
    having count(*) = 1
) m
where c.hull is null and c.run_key = m.run_key and c.finding_id = m.finding_id;
"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(env("STS_DATABASE_URL"))
    conn.execute("set time zone 'UTC'")
    return conn


def init(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)
    conn.execute(LINKS)
    conn.commit()


def counts(conn: psycopg.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema = 'review' and table_type = 'BASE TABLE' order by 1")]
    return {t: conn.execute(f"select count(*) from review.{t}").fetchone()[0] for t in tables}


EVENT_KEYS = {
    "scan_sources": "scan_id",
    "observations": "scan_id",
    "jobs": "scan_id",
    "job_attempts": "job_id",
    "job_scenes": "job_id",
}
EVENT_ROOTS = ("scans", "channels")


def base_tables(conn: psycopg.Connection, schema: str) -> list[str]:
    rows = conn.execute("select table_name from information_schema.tables where table_schema = %s and table_type = 'BASE TABLE' order by 1", (schema,))
    return [r[0] for r in rows]


def columns(conn: psycopg.Connection, schema: str, table: str) -> set[str]:
    rows = conn.execute("select column_name from information_schema.columns where table_schema = %s and table_name = %s", (schema, table))
    return {r[0] for r in rows}


def resolve(conn: psycopg.Connection, run: int | None, attempt: str | None, job: str | None, scan: str | None) -> dict[str, list]:
    if run is not None:
        where, arg = "r.run_no = %s", run
    elif attempt:
        where, arg = "r.attempt_id = %s", attempt
    elif job:
        where, arg = "l.job_id = %s", job
    elif scan:
        where, arg = "l.scan_id = %s", scan
    else:
        raise ValueError("give one of run, attempt, job or scan")
    rows = conn.execute(
        "select r.run_key, r.run_no, l.attempt_id, l.job_id, l.scan_id from review.runs r "
        f"left join review.run_lineage l using (run_key) where {where} order by r.run_no",
        (arg,),
    ).fetchall()
    if not rows and (job or scan):
        found = conn.execute(
            "select j.job_id, j.scan_id from processing.jobs j where j.job_id = %s or j.scan_id = %s",
            (job, scan),
        ).fetchall()
        return {"run_keys": [], "run_nos": [], "attempts": [], "jobs": sorted({r[0] for r in found}), "scans": sorted({r[1] for r in found if r[1]})}
    return {
        "run_keys": [r[0] for r in rows],
        "run_nos": [r[1] for r in rows],
        "attempts": sorted({r[2] for r in rows if r[2]}),
        "jobs": sorted({r[3] for r in rows if r[3]}),
        "scans": sorted({r[4] for r in rows if r[4]}),
    }


def trace(conn: psycopg.Connection, run: int | None = None, attempt: str | None = None, job: str | None = None, scan: str | None = None) -> dict:
    found = resolve(conn, run, attempt, job, scan)
    keys, jobs = found["run_keys"], found["jobs"]
    review = {}
    for table in base_tables(conn, "review"):
        if "run_key" in columns(conn, "review", table):
            review[table] = conn.execute(f"select count(*) from review.{table} where run_key = any(%s)", (keys,)).fetchone()[0]
    ledger = {
        "scans": len(found["scans"]),
        "jobs": len(jobs),
        "job_attempts": conn.execute("select count(*) from processing.job_attempts where job_id = any(%s)", (jobs,)).fetchone()[0],
        "job_scenes": conn.execute("select count(*) from processing.job_scenes where job_id = any(%s)", (jobs,)).fetchone()[0],
        "observations": conn.execute(
            "select count(distinct (o.site, o.source, o.scene, o.version)) from processing.observations o "
            "join processing.job_scenes s on s.site = o.site and s.source = o.source and s.scene = o.scene where s.job_id = any(%s)",
            (jobs,),
        ).fetchone()[0],
    }
    return found | {"review": review, "processing": ledger}


def check(conn: psycopg.Connection) -> list[str]:
    problems = []
    for table in base_tables(conn, "review"):
        cols = columns(conn, "review", table)
        if table != "runs" and "run_key" not in cols and "first_run_key" not in cols:
            problems.append(f"review.{table} has no run_key, so its rows cannot be traced to the run that produced them")
        elif table != "runs":
            links = conn.execute("select count(*) from pg_constraint where contype = 'f' and conrelid = %s::regclass", (f"review.{table}",)).fetchone()[0]
            if not links:
                problems.append(f"review.{table} has a run key but no foreign key to enforce it")
    for table in base_tables(conn, "processing"):
        key = EVENT_KEYS.get(table)
        if table in EVENT_ROOTS:
            continue
        if key is None or key not in columns(conn, "processing", table):
            problems.append(f"processing.{table} has no event key (scan_id or job_id)")
    probes = {
        "review flags not linked to a hull": "select count(*) from review.qa_flags where site is null or hull is null",
        "captures not linked to a hull (published before links existed)": "select count(*) from review.captures where hull is null",
        "job runs without the attempt that produced them": "select count(*) from review.runs where origin = 'job' and attempt_id is null",
        "observations without the scan that found them": "select count(*) from processing.observations where scan_id is null",
        "jobs without the scan that created them": "select count(*) from processing.jobs where scan_id is null",
        "jobs without their scenes": "select count(*) from processing.jobs j where not exists (select 1 from processing.job_scenes s where s.job_id = j.job_id)",
        "succeeded attempts without a run": "select count(*) from processing.job_attempts a where a.status = 'succeeded' and not exists (select 1 from review.runs r where r.attempt_id = a.attempt_id)",
    }
    for label, sql in probes.items():
        n = conn.execute(sql).fetchone()[0]
        if n:
            problems.append(f"{n} {label}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor pg")
    parser.add_argument("command", choices=("init", "counts", "trace", "check"))
    parser.add_argument("--run", type=int)
    parser.add_argument("--attempt")
    parser.add_argument("--job")
    parser.add_argument("--scan")
    args = parser.parse_args()
    with connect() as conn:
        if args.command == "check":
            problems = check(conn)
            for line in problems:
                print("FAIL", line)
            print("check: " + ("every table traces to its event" if not problems else f"{len(problems)} problem(s)"))
            return 1 if problems else 0
        if args.command == "trace":
            found = trace(conn, args.run, args.attempt, args.job, args.scan)
            print("event chain:", {k: found[k] for k in ("scans", "jobs", "attempts", "run_nos")})
            for schema in ("processing", "review"):
                print(f"{schema}:", ", ".join(f"{t} {n}" for t, n in found[schema].items() if n) or "nothing")
            return 0
        if args.command == "init":
            init(conn)
        for table, n in counts(conn).items():
            print(f"{table:18} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
