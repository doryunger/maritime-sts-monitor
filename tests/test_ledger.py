import json

from maritime_tip_cue import ledger


def add_job(conn, job_id="j1", status="queued", retries=0):
    conn.execute(
        "insert into processing.jobs "
        "(job_id, site, source, created_utc, manifest_json, manifest_sha256, status, retry_count) "
        "values (%s, 'fujairah_outer', 's2', now(), %s, 'x', %s, %s)",
        (job_id, json.dumps({"job_id": job_id}), status, retries),
    )


def test_first_claim_records_the_attempt_and_marks_the_job_running(pgdb):
    add_job(pgdb, retries=1)
    assert ledger.ensure_attempt(pgdb, "j1", "a1", "worker-1") is True
    attempt = pgdb.execute(
        "select job_id, attempt_no, owner, status from processing.job_attempts where attempt_id = 'a1'"
    ).fetchone()
    assert attempt == ("j1", 2, "worker-1", "running")
    job = pgdb.execute("select status, current_attempt_id from processing.jobs where job_id = 'j1'").fetchone()
    assert job == ("running", "a1")


def test_a_second_call_for_the_same_attempt_changes_nothing(pgdb):
    add_job(pgdb)
    assert ledger.ensure_attempt(pgdb, "j1", "a1", "worker-1") is True
    assert ledger.ensure_attempt(pgdb, "j1", "a1", "manager") is False
    assert pgdb.execute("select count(*), min(owner) from processing.job_attempts").fetchone() == (1, "worker-1")


def test_a_job_that_is_already_running_or_finished_takes_no_new_attempt(pgdb):
    for status in ("running", "succeeded", "failed"):
        add_job(pgdb, job_id=f"j-{status}", status=status)
        assert ledger.ensure_attempt(pgdb, f"j-{status}", f"a-{status}", "w") is False
    assert pgdb.execute("select count(*) from processing.job_attempts").fetchone()[0] == 0


def test_an_unknown_job_is_ignored(pgdb):
    assert ledger.ensure_attempt(pgdb, "nope", "a1", "w") is False
    assert pgdb.execute("select count(*) from processing.job_attempts").fetchone()[0] == 0
