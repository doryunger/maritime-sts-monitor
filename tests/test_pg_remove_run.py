import pytest

from maritime_tip_cue import pg
from tests.test_s3runs import job_run


def attempt_of(conn, run_no):
    return conn.execute("select attempt_id from review.runs where run_no = %s", (run_no,)).fetchone()[0]


def succeeded_without_a_run(problems):
    return [p for p in problems if "succeeded attempts without a run" in p]


def test_removing_a_run_deletes_it_and_records_the_attempt_so_check_stays_clean(pgdb):
    run = job_run(pgdb)
    pgdb.commit()
    assert not succeeded_without_a_run(pg.check(pgdb))
    result = pg.remove_run(pgdb, "test job with no images", run=run["run_no"], by="me")
    pgdb.commit()
    assert result["recorded"] is True and result["attempt_id"]
    assert pgdb.execute("select count(*) from review.runs").fetchone()[0] == 0
    assert pgdb.execute("select reason, removed_by from review.removed_runs").fetchone() == ("test job with no images", "me")
    assert not succeeded_without_a_run(pg.check(pgdb))


def test_a_run_with_flags_is_refused_and_stays(pgdb):
    run = job_run(pgdb)
    pgdb.execute("insert into review.qa_flags (run_key, hull_id, status) values (%s, 'h', 'ok')", (run["run_key"],))
    pgdb.commit()
    with pytest.raises(ValueError, match="qa_flags"):
        pg.remove_run(pgdb, "no", run=run["run_no"])
    assert pgdb.execute("select count(*) from review.runs").fetchone()[0] == 1


def test_an_already_removed_run_can_be_recorded_by_its_attempt(pgdb):
    run = job_run(pgdb)
    attempt = run["attempt_id"]
    pgdb.execute("delete from review.runs where run_key = %s", (run["run_key"],))
    pgdb.commit()
    assert succeeded_without_a_run(pg.check(pgdb))
    assert pg.remove_run(pgdb, "removed before this command existed", attempt=attempt)["recorded"] is True
    pgdb.commit()
    assert not succeeded_without_a_run(pg.check(pgdb))
    assert pg.remove_run(pgdb, "again", attempt=attempt)["recorded"] is False


def test_attempt_mode_refuses_a_live_run_an_unknown_attempt_and_ambiguous_arguments(pgdb):
    run = job_run(pgdb)
    with pytest.raises(ValueError, match="still exists"):
        pg.remove_run(pgdb, "x", attempt=run["attempt_id"])
    with pytest.raises(ValueError, match="unknown attempt"):
        pg.remove_run(pgdb, "x", attempt="nope")
    with pytest.raises(ValueError, match="exactly one"):
        pg.remove_run(pgdb, "x")
