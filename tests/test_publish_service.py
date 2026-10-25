import json

from maritime_tip_cue import publish, publish_service, publish_sync
from maritime_tip_cue.sts import rules as rules_mod
from tests.pgstore import add_scene, hull, new_store

RULES = rules_mod.load()


def add_request(conn, status="pending", dry_run=False, started=None):
    row = conn.execute(
        "insert into review.sync_runs (requested_by, status, dry_run, started_utc) values ('u', %s, %s, %s) returning sync_id",
        (status, dry_run, started),
    ).fetchone()
    conn.commit()
    return row[0]


def row_of(conn, sync_id):
    conn.rollback()
    return conn.execute(
        "select status, started_utc, finished_utc, summary, error from review.sync_runs where sync_id = %s", (sync_id,)
    ).fetchone()


def stub(summary=None, error=None, calls=None):
    def run(client, bucket, requested, upload, dry_run, rules, sync_id):
        if calls is not None:
            calls.append((requested, upload, dry_run, sync_id, bucket))
        if error:
            raise error
        return ["line"], summary or {"totals": {"added": 1}}

    return run


def test_nothing_pending_is_a_no_op(pgdb):
    calls = []
    assert publish_service.run_pending(pgdb, object(), "b", RULES, stub(calls=calls)) is None
    assert calls == []


def test_oldest_pending_is_claimed_run_with_upload_and_stored_done(pgdb):
    first, second = add_request(pgdb), add_request(pgdb)
    calls = []
    assert publish_service.run_pending(pgdb, object(), "b", RULES, stub({"totals": {"added": 3}}, calls=calls)) == "done"
    assert calls == [([], True, False, first, "b")]
    status, started, finished, summary, error = row_of(pgdb, first)
    assert (status, error) == ("done", None) and started and finished and summary == {"totals": {"added": 3}}
    assert row_of(pgdb, second)[0] == "pending"


def test_dry_run_request_does_not_upload(pgdb):
    add_request(pgdb, dry_run=True)
    calls = []
    publish_service.run_pending(pgdb, object(), "b", RULES, stub(calls=calls))
    assert calls[0][1:3] == (False, True)


def test_failure_stores_the_error_and_a_rerun_can_be_requested(pgdb):
    sync_id = add_request(pgdb)
    assert publish_service.run_pending(pgdb, object(), "b", RULES, stub(error=RuntimeError("s3 down"))) == "failed"
    status, _, finished, summary, error = row_of(pgdb, sync_id)
    assert status == "failed" and finished and summary is None and "s3 down" in error
    again = add_request(pgdb)
    assert publish_service.run_pending(pgdb, object(), "b", RULES, stub()) == "done"
    assert row_of(pgdb, again)[0] == "done"


def test_systemexit_message_is_the_error(pgdb):
    sync_id = add_request(pgdb)
    publish_service.run_pending(pgdb, object(), "b", RULES, stub(error=SystemExit("Refusing to sync")))
    assert row_of(pgdb, sync_id)[4] == "Refusing to sync"


def test_stale_running_request_is_abandoned_and_a_fresh_one_is_kept(pgdb):
    stale = add_request(pgdb, "running", started="2000-01-01T00:00:00Z")
    fresh = pgdb.execute("insert into review.sync_runs (status, started_utc) values ('running', now()) returning sync_id").fetchone()[0]
    pgdb.commit()
    assert publish_service.run_pending(pgdb, object(), "b", RULES, stub()) is None
    status, _, finished, _, error = row_of(pgdb, stale)
    assert (status, error) == ("failed", "abandoned") and finished
    assert row_of(pgdb, fresh)[0] == "running"


def test_outputs_and_captures_recorded_by_a_sync_carry_its_sync_id(pgdb, monkeypatch, tmp_path):
    store = new_store(monkeypatch, tmp_path)
    add_scene(store, hulls=[hull("a")])
    sync_id = add_request(pgdb, "running")
    entries = {
        1: [
            ("capture", "f1", "png", "/x/f1.png", "s3://b/c/f1.png", "s", "x", "a"),
            ("output", "/x/o.parquet", "s3://b/o.parquet", "01-10-2026", "x", "s2", 1, 1, ["s"], "sha"),
        ]
    }
    publish.record_entries(entries, "2026-10-25T00:00:00Z", True, sync_id)
    assert pgdb.execute("select sync_id from review.captures").fetchall() == [(sync_id,)]
    assert pgdb.execute("select sync_id from review.outputs").fetchall() == [(sync_id,)]
    publish.record_entries(entries, "2026-10-25T00:00:00Z", True)
    assert pgdb.execute("select sync_id from review.outputs").fetchall() == [(None,)]
    store.close()


def test_summary_is_structured_per_file(monkeypatch, pgdb):
    monkeypatch.setattr(publish_sync, "choose_runs", lambda requested: [])
    monkeypatch.setattr(publish_sync, "gate", lambda runs, explicit: ([], {3: "not done"}))
    monkeypatch.setattr(publish_sync, "read_published", lambda client, bucket, prefix: {})
    lines, summary = publish_sync.sync_report(object(), "b", [], True, False, RULES, 5)
    assert summary["runs_skipped"] == {"3": "not done"} and summary["totals"]["added"] == 0 and summary["files"] == {}
    assert json.dumps(summary)
