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
"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(env("STS_DATABASE_URL"))
    conn.execute("set time zone 'UTC'")
    return conn


def init(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def counts(conn: psycopg.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema = 'review' and table_type = 'BASE TABLE' order by 1")]
    return {t: conn.execute(f"select count(*) from review.{t}").fetchone()[0] for t in tables}


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor pg")
    parser.add_argument("command", choices=("init", "counts"))
    args = parser.parse_args()
    with connect() as conn:
        if args.command == "init":
            init(conn)
        for table, n in counts(conn).items():
            print(f"{table:18} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
