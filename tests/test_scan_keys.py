import json
from contextlib import closing

import fakeredis
import pytest
from psycopg.types.json import Jsonb

from maritime_tip_cue import catalogue, ledger, pg, task_queue

SITE = {"name": "site-a", "bbox": [0, 0, 1, 1], "min_shore_distance_m": 1000}
JOB = "j" * 64


def s1_id(second):
    return f"S1A_IW_GRDH_1SDV_20261021T0200{second}_20261021T0201{second}_054321_06A2B3_ABCD"


def scene_row(source, scene):
    return {
        "site": SITE["name"],
        "source": source,
        "scene": scene,
        "time": "2026-10-22T02:00:00Z",
        "cloud": None,
        "overlap_km2": 12.5,
        "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
    }


@pytest.fixture
def scanning(monkeypatch, pgdb, tmp_path):
    found = {"s1": [], "s2": []}
    monkeypatch.setattr(catalogue.hotspots, "load", lambda: [SITE])
    monkeypatch.setattr(
        catalogue.passes, "find", lambda start, end, sites, rules=None, sources=None: list(found[sources[0]])
    )
    cfg = {"log_path": str(tmp_path / "scans.jsonl")}
    return pgdb, found, cfg


def test_scan_stamps_observations_jobs_and_job_scenes_with_the_scan_that_made_them(scanning):
    db, found, cfg = scanning
    first_slice, second_slice = s1_id("00"), s1_id("25")
    found["s1"] = [scene_row("s1", first_slice)]
    found["s2"] = [scene_row("s2", "S2B_X_T40REN_A")]
    baseline = catalogue.scan(db, cfg)
    assert baseline["status"] == "ok"
    assert db.execute("select count(*) from processing.jobs").fetchone()[0] == 0
    stamped = db.execute("select count(*) from processing.observations where scan_id = %s", (baseline["scan_id"],))
    assert stamped.fetchone()[0] == 2

    found["s1"] = [scene_row("s1", first_slice), scene_row("s1", second_slice)]
    found["s2"] = [scene_row("s2", "S2B_X_T40REN_A"), scene_row("s2", "S2B_X_T40REN_B")]
    later = catalogue.scan(db, cfg)

    observed = dict(db.execute("select scene, scan_id from processing.observations").fetchall())
    assert observed[first_slice] == baseline["scan_id"] and observed["S2B_X_T40REN_A"] == baseline["scan_id"]
    assert observed[second_slice] == later["scan_id"] and observed["S2B_X_T40REN_B"] == later["scan_id"]

    jobs = db.execute("select job_id, source, scan_id from processing.jobs order by source").fetchall()
    assert [(j[1], j[2]) for j in jobs] == [("s1", later["scan_id"]), ("s2", later["scan_id"])]
    scenes = {
        (job_id, scene) for job_id, scene in db.execute("select job_id, scene from processing.job_scenes").fetchall()
    }
    assert scenes == {(jobs[0][0], first_slice), (jobs[0][0], second_slice), (jobs[1][0], "S2B_X_T40REN_B")}
    sources = db.execute("select scan_id, source, new from processing.scan_sources order by scan_id, source").fetchall()
    assert sorted(sources, key=lambda r: (r[0] != baseline["scan_id"], r[1])) == [
        (baseline["scan_id"], "s1", 1),
        (baseline["scan_id"], "s2", 1),
        (later["scan_id"], "s1", 1),
        (later["scan_id"], "s2", 1),
    ]


def test_a_repeat_scan_keeps_the_first_scan_on_rows_it_sees_again(scanning):
    db, found, cfg = scanning
    found["s2"] = [scene_row("s2", "S2B_X_T40REN_A")]
    first = catalogue.scan(db, cfg)
    again = catalogue.scan(db, cfg)
    assert again["scan_id"] != first["scan_id"]
    assert db.execute("select scan_id from processing.observations").fetchall() == [(first["scan_id"],)]


def test_every_derived_row_of_a_new_job_traces_back_to_its_scan(scanning):
    db, found, cfg = scanning
    catalogue.scan(db, cfg)
    found["s2"] = [scene_row("s2", "S2B_X_T40REN_B")]
    later = catalogue.scan(db, cfg)
    job_id = db.execute("select job_id from processing.jobs").fetchone()[0]
    traced = pg.trace(db, job=job_id)
    assert traced["scans"] == [later["scan_id"]] and traced["jobs"] == [job_id]
    assert traced["processing"]["job_scenes"] == 1 and traced["processing"]["observations"] == 1


def test_the_controller_lock_admits_one_holder_at_a_time(pgdb):
    with closing(catalogue.connect()) as first, closing(catalogue.connect()) as second:
        with catalogue.controller_lock(first):
            with pytest.raises(BlockingIOError):
                with catalogue.controller_lock(second):
                    pass
        with catalogue.controller_lock(second):
            with pytest.raises(BlockingIOError):
                with catalogue.controller_lock(first):
                    pass


def test_the_controller_lock_is_released_when_its_holder_fails(pgdb):
    with closing(catalogue.connect()) as first, closing(catalogue.connect()) as second:
        with pytest.raises(RuntimeError):
            with catalogue.controller_lock(first):
                raise RuntimeError("scan failed")
        with catalogue.controller_lock(second):
            pass


def test_a_dropped_holder_connection_releases_the_lock(pgdb):
    first = catalogue.connect()
    with closing(catalogue.connect()) as second:
        with catalogue.controller_lock(first):
            with pytest.raises(BlockingIOError):
                with catalogue.controller_lock(second):
                    pass
            first.close()
        with catalogue.controller_lock(second):
            pass


@pytest.fixture
def claims(monkeypatch, pgdb):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    manifest = {"job_id": JOB, "site": {"name": "site-a"}, "scenes": [{"scene": "s"}]}
    pgdb.execute(
        "insert into processing.jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status,retry_count) "
        "values (%s,'site-a','s2',now(),%s,%s,'dispatched',1)",
        (JOB, Jsonb(manifest), JOB),
    )
    pgdb.commit()
    return pgdb, server


def claimed(attempt, owner="manager-seen-owner"):
    return [("1-0", {"type": "claimed", "job_id": JOB, "attempt_id": attempt, "owner": owner})]


def reconcile(db, server, events):
    return catalogue.reconcile_redis(db, server, None, "bucket", "catalogue/artifacts/", events, 1, 300)


def test_the_manager_records_the_attempt_and_marks_the_job_running_when_it_sees_the_claim_first(claims):
    db, server = claims
    reconcile(db, server, claimed("a1"))
    assert db.execute("select job_id, attempt_no, owner, status from processing.job_attempts").fetchall() == [
        (JOB, 2, "manager-seen-owner", "running")
    ]
    assert db.execute("select status, current_attempt_id from processing.jobs").fetchone() == ("running", "a1")
    assert ledger.ensure_attempt(db, JOB, "a1", "worker-1") is False
    assert db.execute("select count(*) from processing.job_attempts").fetchone()[0] == 1


def test_the_manager_leaves_the_worker_recorded_attempt_alone(claims):
    db, server = claims
    assert ledger.ensure_attempt(db, JOB, "a1", "worker-1") is True
    db.commit()
    reconcile(db, server, claimed("a1"))
    assert db.execute("select owner, status from processing.job_attempts").fetchall() == [("worker-1", "running")]
    assert db.execute("select status, current_attempt_id from processing.jobs").fetchone() == ("running", "a1")


def test_a_claim_for_a_finished_job_records_nothing(claims):
    db, server = claims
    db.execute("update processing.jobs set status = 'succeeded'")
    db.commit()
    reconcile(db, server, claimed("a1"))
    assert db.execute("select count(*) from processing.job_attempts").fetchone()[0] == 0


def test_claim_then_result_in_one_batch_spends_the_last_retry(claims):
    db, server = claims
    task_queue.restore({JOB: {"job_id": JOB, "site": {"name": "site-a"}, "scenes": [{"scene": "s"}]}}, [])
    receipt = {
        "schema_version": 1, "job_id": JOB, "attempt_id": "a1", "manifest_sha256": JOB, "status": "failed",
        "artifacts": [], "scene_counts": {"checked": 0, "failed": 1}, "error": "boom",
    }
    server.set(task_queue.keys()["result"] + JOB + ":a1", json.dumps(receipt))
    events = claimed("a1", "w") + [("2-0", {"type": "result", "job_id": JOB, "attempt_id": "a1", "owner": "w"})]
    reconcile(db, server, events)
    status = catalogue.job_status(db, JOB)
    assert status["status"] == "failed" and [a["verdict"] for a in status["attempts"]] == ["failed"]
