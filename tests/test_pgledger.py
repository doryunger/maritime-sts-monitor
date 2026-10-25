import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maritime_tip_cue import catalogue, pg, pgledger

REAL = Path(__file__).resolve().parents[1] / "data" / "test_inputs" / "tracker.db"
SITE = {"name": "site-a", "bbox": [0, 0, 1, 1], "min_shore_distance_m": 1000}
RULES = {"scene": {"x": {"value": 1}}}

DDL = """
CREATE TABLE scans (scan_id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, ended_utc TEXT, status TEXT NOT NULL,
    summary_json TEXT);
CREATE TABLE channels (site TEXT NOT NULL, source TEXT NOT NULL, baseline_utc TEXT NOT NULL,
    PRIMARY KEY (site, source));
CREATE TABLE observations (site TEXT NOT NULL, source TEXT NOT NULL, scene TEXT NOT NULL, version TEXT NOT NULL,
    processing_group TEXT NOT NULL, first_discovered_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
    row_json TEXT NOT NULL, PRIMARY KEY (site, source, scene, version));
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, site TEXT NOT NULL, source TEXT NOT NULL, created_utc TEXT NOT NULL,
    manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, status TEXT NOT NULL, dispatched_utc TEXT,
    completed_utc TEXT, error TEXT, receipt_json TEXT, retry_count INTEGER NOT NULL DEFAULT 0, available_utc TEXT,
    current_attempt_id TEXT, superseded_by TEXT);
CREATE TABLE job_attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
    owner TEXT NOT NULL, status TEXT NOT NULL, started_utc TEXT NOT NULL, finished_utc TEXT, error TEXT,
    receipt_json TEXT);
CREATE TABLE scan_sources (scan_id TEXT NOT NULL, site TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
    matched INTEGER NOT NULL, new INTEGER NOT NULL, baseline INTEGER NOT NULL, error TEXT,
    PRIMARY KEY (scan_id, site, source));
"""

T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)


def stamp(delta):
    return (T0 + timedelta(seconds=delta)).isoformat()


def scene(name):
    return {"site": SITE["name"], "source": "s2", "scene": name, "time": "2026-10-19T06:52:00Z", "cloud": 3.5}


def manifest(*names):
    return catalogue.make_manifest(SITE, [scene(n) for n in names], RULES)


M1, M2, M3 = manifest("S2_A"), manifest("S2_B", "S2_C"), manifest("S2_D")
J1, J2, J3 = M1["job_id"], M2["job_id"], M3["job_id"]
A1 = "a1" * 16
A2 = "b2" * 4 + "1" * 24
A3 = "b2" * 4 + "2" * 24
SCAN_1, SCAN_2 = "1" * 32, "2" * 32


def folder(job_id, attempt_id):
    return f"{job_id[:12]}-{attempt_id[:8]}"


@pytest.fixture
def tracker(tmp_path):
    path = tmp_path / "tracker.db"
    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.executemany(
        "insert into scans values (?,?,?,?,?)",
        [
            (SCAN_1, stamp(0), stamp(5), "ok", json.dumps({"scan_id": SCAN_1})),
            (SCAN_2, stamp(7200), stamp(7204), "ok", json.dumps({"scan_id": SCAN_2})),
        ],
    )
    db.execute("insert into channels values (?,?,?)", (SITE["name"], "s2", stamp(2)))
    for name, moment in (("S2_A", 2), ("S2_B", 7203), ("S2_C", 7203), ("S2_D", 7203)):
        db.execute(
            "insert into observations values (?,?,?,?,?,?,?,?)",
            (SITE["name"], "s2", name, "v1", name, stamp(moment), stamp(moment), json.dumps(scene(name))),
        )
    for job_id, mf, moment, status in ((J1, M1, 3, "succeeded"), (J2, M2, 3600, "pending"), (J3, M3, 7205.5, "failed")):
        db.execute(
            "insert into jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status) "
            "values (?,?,?,?,?,?,?)",
            (job_id, SITE["name"], "s2", stamp(moment), json.dumps(mf), job_id, status),
        )
    db.executemany(
        "insert into job_attempts values (?,?,?,?,?,?,?,?,?)",
        [
            (A1, J1, 1, "w", "succeeded", stamp(10), stamp(20), None, json.dumps({"status": "succeeded"})),
            (A2, J3, 1, "w", "failed", stamp(7300), stamp(7310), "boom", None),
            (A3, J3, 2, "w", "failed", stamp(7400), stamp(7410), "boom", None),
        ],
    )
    db.executemany(
        "insert into scan_sources values (?,?,?,?,?,?,?,?)",
        [(SCAN_1, SITE["name"], "s2", "ok", 1, 1, 0, None), (SCAN_2, SITE["name"], "s2", "ok", 3, 3, 0, None)],
    )
    db.commit()
    db.close()
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_import(path, capsys, dry_run=False):
    assert pgledger.run_import(path, dry_run) == 0
    return json.loads(capsys.readouterr().out)


def add_runs(db, *folders):
    for name in folders:
        origin = "legacy" if name.startswith("legacy-") else "job"
        db.execute("insert into review.runs (folder, origin) values (%s, %s)", (name, origin))
    db.commit()


def test_every_table_is_copied_and_counted(tracker, pgdb, capsys):
    report = run_import(tracker, capsys)
    assert {t: (r["source"], r["inserted"], r["skipped"]) for t, r in report["tables"].items()} == {
        "scans": (2, 2, 0),
        "channels": (1, 1, 0),
        "jobs": (3, 3, 0),
        "job_attempts": (3, 3, 0),
        "observations": (4, 4, 0),
        "scan_sources": (2, 2, 0),
    }
    assert pgdb.execute("select count(*) from processing.jobs").fetchone()[0] == 3
    receipt = pgdb.execute("select receipt_json from processing.job_attempts where attempt_id = %s", (A1,))
    assert receipt.fetchone()[0] == {"status": "succeeded"}


def test_scan_ids_come_from_the_scan_whose_window_holds_the_moment(tracker, pgdb, capsys):
    report = run_import(tracker, capsys)
    assert report["scan_ids"]["observations"]["contained"] == 4
    assert report["scan_ids"]["jobs"] == {
        "contained": 1,
        "preceding": 1,
        "ambiguous": 0,
        "unmatched": 1,
        "ambiguous_examples": [],
        "unmatched_examples": [J2],
    }
    assert report["scan_ids"]["scan_sources_new_counts"] == []
    assert dict(pgdb.execute("select scene, scan_id from processing.observations").fetchall()) == {
        "S2_A": SCAN_1, "S2_B": SCAN_2, "S2_C": SCAN_2, "S2_D": SCAN_2,
    }
    assert dict(pgdb.execute("select job_id, scan_id from processing.jobs").fetchall()) == {
        J1: SCAN_1, J2: None, J3: SCAN_2,
    }


def test_job_scenes_hold_every_scene_of_every_manifest(tracker, pgdb, capsys):
    report = run_import(tracker, capsys)
    assert report["job_scenes"] == {"jobs": 3, "inserted": 4, "mismatched": 0, "mismatched_jobs": []}
    assert set(pgdb.execute("select job_id, site, source, scene from processing.job_scenes").fetchall()) == {
        (J1, "site-a", "s2", "S2_A"),
        (J2, "site-a", "s2", "S2_B"),
        (J2, "site-a", "s2", "S2_C"),
        (J3, "site-a", "s2", "S2_D"),
    }


def test_a_second_import_changes_nothing_and_the_source_file_is_never_modified(tracker, pgdb, capsys):
    before = sha(tracker)
    run_import(tracker, capsys)
    tables = [*pg.EVENT_KEYS, "scans", "channels"]
    first = {t: pgdb.execute(f"select count(*) from processing.{t}").fetchone()[0] for t in tables}
    pgdb.commit()
    again = run_import(tracker, capsys)
    assert all(r["inserted"] == 0 and r["skipped"] == r["source"] for r in again["tables"].values())
    assert again["job_scenes"]["inserted"] == 0
    assert again["scan_ids"]["observations"]["contained"] == 0
    assert {t: pgdb.execute(f"select count(*) from processing.{t}").fetchone()[0] for t in first} == first
    assert sha(tracker) == before


def test_a_dry_run_rolls_everything_back(tracker, pgdb, capsys):
    report = run_import(tracker, capsys, dry_run=True)
    assert report["dry_run"] == "rolled back" and report["tables"]["jobs"]["inserted"] == 3
    assert pgdb.execute("select count(*) from processing.jobs").fetchone()[0] == 0


def test_a_failing_check_leaves_the_database_untouched(tracker, pgdb, capsys, monkeypatch):
    def broken(conn):
        raise RuntimeError("derivation failed")

    monkeypatch.setattr(pgledger, "derive_job_scenes", broken)
    with pytest.raises(RuntimeError):
        pgledger.run_import(tracker, False)
    assert pgdb.execute("select count(*) from processing.jobs").fetchone()[0] == 0


def test_runs_are_linked_to_the_one_attempt_their_folder_names(tracker, pgdb, capsys):
    ghost = folder("f" * 64, "e" * 32)
    add_runs(pgdb, folder(J1, A1), folder(J3, A2), ghost, "legacy-run-3")
    report = run_import(tracker, capsys)
    linked = dict(pgdb.execute("select folder, attempt_id from review.runs").fetchall())
    assert linked[folder(J1, A1)] == A1
    assert linked[folder(J3, A2)] is None and linked[ghost] is None and linked["legacy-run-3"] is None
    assert report["runs"]["linked"] == 1
    assert report["runs"]["several_count"] == 1 and report["runs"]["no_match_count"] == 1
    assert report["runs"]["legacy_left_null"] == 1


def test_runs_added_later_are_linked_by_the_next_import(tracker, pgdb, capsys):
    run_import(tracker, capsys)
    add_runs(pgdb, folder(J1, A1))
    report = run_import(tracker, capsys)
    assert report["runs"]["linked"] == 1
    assert pgdb.execute("select attempt_id from review.runs").fetchone()[0] == A1


def test_verify_passes_after_the_import_and_fails_when_a_row_is_lost(tracker, pgdb, capsys):
    run_import(tracker, capsys)
    assert pgledger.run_verify(tracker) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tables"]["jobs"] == {"source": 3, "postgres": 3, "missing": 0}
    assert report["unresolved"]["jobs_without_scan"] == 1
    pgdb.execute("delete from processing.job_scenes where job_id = %s", (J2,))
    pgdb.execute("delete from processing.job_attempts where attempt_id = %s", (A3,))
    pgdb.commit()
    assert pgledger.run_verify(tracker) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert problems["rows_missing"] == {"job_attempts": 1} and problems["job_scenes_incomplete"] == [J2]


def test_scan_assignment_never_guesses():
    scans = [
        ("a", T0, T0 + timedelta(seconds=10)),
        ("b", T0 + timedelta(seconds=5), T0 + timedelta(seconds=20)),
        ("c", T0 + timedelta(seconds=100), None),
    ]
    found = pgledger.assign_scans(
        scans,
        {
            "early": T0 - timedelta(seconds=1),
            "both": T0 + timedelta(seconds=7),
            "only_b": T0 + timedelta(seconds=15),
            "just_after_b": T0 + timedelta(seconds=30),
            "long_after_b": T0 + timedelta(seconds=80),
            "inside_open_scan": T0 + timedelta(seconds=500),
        },
    )
    assert found["assigned"] == {"only_b": "b", "just_after_b": "b", "inside_open_scan": "c"}
    assert found["ambiguous"] == ["both"]
    assert found["unmatched"] == ["early", "long_after_b"]


def chain(tracker, pgdb, capsys):
    add_runs(pgdb, folder(J1, A1))
    run_import(tracker, capsys)
    pgdb.execute(
        "insert into review.scenes (run_key, scene_id, site) select run_key, 'S2_A', 'site-a' from review.runs"
    )
    pgdb.commit()


def test_trace_follows_a_job_and_a_scan_through_to_the_run(tracker, pgdb, capsys):
    chain(tracker, pgdb, capsys)
    by_job = pg.trace(pgdb, job=J1)
    assert (by_job["scans"], by_job["jobs"], by_job["attempts"], by_job["run_nos"]) == ([SCAN_1], [J1], [A1], [1])
    assert by_job["review"]["scenes"] == 1
    assert by_job["processing"] == {"scans": 1, "jobs": 1, "job_attempts": 1, "job_scenes": 1, "observations": 1}
    by_scan = pg.trace(pgdb, scan=SCAN_1)
    assert by_scan["jobs"] == [J1] and by_scan["run_nos"] == [1] and by_scan["review"]["scenes"] == 1
    assert pg.trace(pgdb, attempt=A1)["jobs"] == [J1]
    quiet = pg.trace(pgdb, scan=SCAN_2)
    assert quiet["jobs"] == [J3] and quiet["run_nos"] == []


def test_the_trace_command_prints_the_chain(tracker, pgdb, capsys, monkeypatch):
    chain(tracker, pgdb, capsys)
    monkeypatch.setattr(sys, "argv", ["pg", "trace", "--job", J1])
    assert pg.main() == 0
    out = capsys.readouterr().out
    assert f"'jobs': ['{J1}']" in out and "job_attempts 1" in out and "scenes 1" in out


def adopt(capsys, jobs, note="jobs created by hand between two scans"):
    code = pgledger.run_adopt(jobs, note)
    return code, capsys.readouterr().out


def test_a_manual_adoption_becomes_its_own_scan_event(tracker, pgdb, capsys):
    add_runs(pgdb, folder(J1, A1), "legacy-run-3")
    run_import(tracker, capsys)
    created = pgdb.execute("select created_utc from processing.jobs where job_id = %s", (J2,)).fetchone()[0]
    code, out = adopt(capsys, [J2])
    scan_id = pgledger.adoption_scan_id(created)
    assert code == 0 and json.loads(out) == {"scan_id": scan_id, "jobs_linked": 1, "jobs": [J2]}
    row = pgdb.execute(
        "select status, started_utc, ended_utc, summary_json from processing.scans where scan_id = %s", (scan_id,)
    ).fetchone()
    assert row[0] == "adopted" and row[1] == created and row[2] == created
    assert row[3] == {"kind": "manual adoption", "note": "jobs created by hand between two scans", "jobs": [J2]}
    assert pgdb.execute("select scan_id from processing.jobs where job_id = %s", (J2,)).fetchone()[0] == scan_id
    assert not [p for p in pg.check(pgdb) if "jobs without the scan" in p]
    assert pg.trace(pgdb, scan=scan_id)["jobs"] == [J2]


def test_adopting_twice_changes_nothing(tracker, pgdb, capsys):
    run_import(tracker, capsys)
    assert adopt(capsys, [J2])[0] == 0
    scans = pgdb.execute("select count(*) from processing.scans").fetchone()[0]
    assert adopt(capsys, [J2])[0] == 0
    assert pgdb.execute("select count(*) from processing.scans").fetchone()[0] == scans


def test_adoption_refuses_jobs_a_scan_already_made_and_unknown_jobs(tracker, pgdb, capsys):
    run_import(tracker, capsys)
    scans = pgdb.execute("select count(*) from processing.scans").fetchone()[0]
    code, out = adopt(capsys, [J1])
    assert code == 1 and json.loads(out) == {"refused": {"jobs_that_already_have_a_scan": [J1]}}
    code, out = adopt(capsys, ["nope"])
    assert code == 1 and json.loads(out) == {"refused": {"unknown_jobs": ["nope"]}}
    assert pgdb.execute("select count(*) from processing.scans").fetchone()[0] == scans


def test_check_names_the_job_the_import_could_not_place(tracker, pgdb, capsys):
    add_runs(pgdb, folder(J1, A1), "legacy-run-3")
    run_import(tracker, capsys)
    problems = pg.check(pgdb)
    assert "1 jobs without the scan that created them" in problems
    assert not [p for p in problems if "observations" in p or "without their scenes" in p or "job runs" in p]


@pytest.mark.skipif(not REAL.exists(), reason="data/test_inputs/tracker.db is not present")
def test_the_real_ledger_imports_with_every_manifest_intact(pgdb, capsys):
    source = sqlite3.connect(f"file:{REAL.as_posix()}?mode=ro", uri=True)
    attempts = source.execute("select job_id, attempt_id, status from job_attempts").fetchall()
    manifests = dict(source.execute("select job_id, manifest_json from jobs").fetchall())
    before = sha(REAL)
    names = [folder(job, attempt) for job, attempt, status in attempts if status == "succeeded"]
    add_runs(pgdb, *names, "legacy-run-1")
    report = run_import(REAL, capsys)
    assert {t: r["source"] for t, r in report["tables"].items()} == {
        "scans": 26, "channels": 8, "jobs": 107, "job_attempts": 72, "observations": 158, "scan_sources": 196,
    }
    assert all(r["inserted"] == r["source"] for r in report["tables"].values())
    assert len(manifests) == 107
    stored = pgdb.execute("select job_id, manifest_json, manifest_sha256 from processing.jobs").fetchall()
    assert len(stored) == 107
    for job_id, value, manifest_sha in stored:
        original = json.loads(manifests[job_id])
        assert catalogue.canonical_bytes(value) == catalogue.canonical_bytes(original)
        core = {k: value[k] for k in ("schema_version", "site", "scenes", "rules", "rules_sha256")}
        assert catalogue.digest(core) == job_id == manifest_sha
    assert report["manifest_digest_mismatches"] == 0
    assert report["scan_ids"]["observations"]["unmatched"] == 0 and report["scan_ids"]["observations"]["ambiguous"] == 0
    assert report["scan_ids"]["jobs"]["ambiguous"] == 0
    assert report["job_scenes"]["mismatched"] == 0
    assert report["runs"]["no_match_count"] == 0 and report["runs"]["several_count"] == 0
    succeeded = sum(1 for _, _, status in attempts if status == "succeeded")
    assert report["runs"]["linked"] == succeeded
    assert pgledger.run_verify(REAL) == 0
    capsys.readouterr()
    again = run_import(REAL, capsys)
    assert all(r["inserted"] == 0 for r in again["tables"].values())
    assert sha(REAL) == before
    print("real ledger:", json.dumps({k: report[k] for k in ("scan_ids", "job_scenes", "runs")}, sort_keys=True))


@pytest.mark.skipif(not REAL.exists(), reason="data/test_inputs/tracker.db is not present")
def test_the_two_hand_adopted_real_jobs_get_one_adoption_event(pgdb, capsys):
    run_import(REAL, capsys)
    unplaced = pgdb.execute(
        "select job_id, site, source from processing.jobs where scan_id is null order by site"
    ).fetchall()
    assert [(r[1], r[2]) for r in unplaced] == [("fujairah_outer", "s1"), ("off_sohar", "s1")]
    code, out = adopt(capsys, [r[0] for r in unplaced], "Gulf catch-up jobs created by hand from the survey manifest")
    assert code == 0 and json.loads(out)["jobs_linked"] == 2
    assert pgdb.execute("select count(*) from processing.jobs where scan_id is null").fetchone()[0] == 0
    scans = pgdb.execute("select scan_id, status from processing.scans where status = 'adopted'").fetchall()
    assert len(scans) == 1 and scans[0][0].startswith("adopted-20261020T175244")
    assert not [p for p in pg.check(pgdb) if "jobs without the scan" in p]
