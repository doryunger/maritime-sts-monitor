import math

import pytest

from app.backend import runs
from maritime_tip_cue import pg, publish
from maritime_tip_cue.watch import registry, report
from maritime_tip_cue.watch.pgutil import iso
from tests.pgstore import add_scene, hull, new_store


@pytest.fixture
def store(pgdb, tmp_path, monkeypatch):
    opened = new_store(monkeypatch, tmp_path)
    yield opened
    opened.close()


def test_a_new_run_takes_the_next_number_and_its_folder_name(pgdb, tmp_path, monkeypatch):
    first = new_store(monkeypatch, tmp_path)
    second = new_store(monkeypatch, tmp_path)
    assert (first.run_no, second.run_no) == (1, 2)
    assert (first.folder, second.folder) == ("legacy-run-1", "legacy-run-2")
    assert second.path == tmp_path / "runs" / "legacy-run-2" and second.path.is_dir()
    assert registry.listing()[1]["run_key"] == second.run_key
    assert registry.folder_of(2) == second.path
    with pytest.raises(KeyError):
        registry.get(9)


def test_a_job_run_carries_its_attempt_and_the_job_folder_name(pgdb, tmp_path, monkeypatch):
    pgdb.execute(
        "insert into processing.jobs (job_id, site, source, created_utc, manifest_json, manifest_sha256, status) "
        "values (%s, 'x', 's2', now(), '{}', 'x', 'running')",
        ("j" * 64,),
    )
    pgdb.execute(
        "insert into processing.job_attempts (attempt_id, job_id, attempt_no, owner, status, started_utc) "
        "values (%s, %s, 1, 'w', 'running', now())",
        ("a" * 32, "j" * 64),
    )
    pgdb.commit()
    job = new_store(monkeypatch, tmp_path, origin="job", attempt_id="a" * 32, job_id="j" * 64)
    assert job.folder == "jjjjjjjjjjjj-aaaaaaaa"
    assert registry.get(job.run_no)["attempt_id"] == "a" * 32
    assert pgdb.execute("select job_id, attempt_no from review.run_lineage where run_key = %s", (job.run_key,)).fetchone() == ("j" * 64, 1)
    from maritime_tip_cue.watch.state import Store

    assert Store.for_attempt("a" * 32).run_key == job.run_key
    assert Store.for_attempt("b" * 32) is None
    with pytest.raises(ValueError):
        registry.new_run("job")


def test_scene_lifecycle_from_begin_to_checked_with_batched_rows(store):
    invocation = store.start_run({"x": 1}, "site", ("a", "b"), "s2", "r", "c")
    scene = {"scene": "S", "site": "x", "source": "s2", "time": "2026-10-01T10:00:00Z"}
    store.begin_scene(invocation, scene, store.path / "scenes" / "s2" / "S", None)
    assert store.scene("S", "x")["status"] == "running" and store.scene("S", "x")["folder"] == "scenes/s2/S"
    features = [{"properties": {"col": i, "row": i, "latitude": 25.0 + i, "longitude": 56.0, "score": math.nan if i == 1 else 0.5}} for i in range(500)]
    store.save_detected(scene, features, {"dets": {"kept": 3}}, 12.0, {"dets": [], "index": [], "nan": math.inf})
    row = store.scene("S", "x")
    assert row["status"] == "detected" and row["data"]["nan"] is None and row["capture_utc"] == "2026-10-01T10:00:00Z"
    assert store.db.execute("select count(*) as n from review.raw_detections where run_key = %s", (store.run_key,)).fetchone()["n"] == 500
    assert store.db.execute("select score from review.raw_detections where run_key = %s and idx = 1", (store.run_key,)).fetchone()["score"] is None
    counts = store.save_checked(scene, [hull("a"), hull("b", "single"), hull("c", "moving"), hull("d", "pair", role="duplicate", extra=math.nan)])
    assert counts["checked"] == 4 and counts["verdicts"] == {"pair": 1, "single": 2} and counts["dets"] == {"kept": 3}
    assert store.scene("S", "x")["status"] == "checked"
    assert [h["verdict"] for h in store.hulls("S", "x")] == ["pair", "single", "moving", "pair"]
    assert store.hulls("S", "x")[3]["extra"] is None
    assert store.n_scenes() == 1 and [s["scene_id"] for s in store.scenes()] == ["S"]
    store.finish_run(invocation, "done")
    assert store.invocations()[0]["status"] == "done" and store.invocations()[0]["ended_utc"].endswith("Z")


def test_a_failed_scene_keeps_its_message_and_a_finished_one_is_not_failed(store):
    add_scene(store, "ok", hulls=["a"])
    broken = {"scene": "bad", "site": "x", "source": "s2", "time": "2026-10-01T11:00:00Z"}
    store.begin_scene(1, broken, store.path / "scenes" / "s2" / "bad", None)
    store.fail(broken, "boom")
    store.fail({"scene": "ok", "site": "x"}, "late")
    assert (store.scene("bad", "x")["status"], store.scene("bad", "x")["error"]) == ("failed", "boom")
    assert store.scene("ok", "x")["status"] == "checked"
    store.begin_scene(1, broken, store.path / "scenes" / "s2" / "bad", None)
    assert store.scene("bad", "x")["status"] == "running" and store.scene("bad", "x")["attempts"] == 2


def test_events_are_numbered_per_run(store):
    store.event(None, "info", None, "first")
    store.event(None, "error", "S", "second")
    rows = store.db.execute("select event_id, level, scene_id, message from review.events where run_key = %s order by event_id", (store.run_key,)).fetchall()
    assert [(r["event_id"], r["message"]) for r in rows] == [(1, "first"), (2, "second")]


def test_a_second_check_of_a_scene_keeps_the_review_flags(store):
    add_scene(store, hulls=["a", "b", "gone"])
    for hull_id in ("s__a", "s__gone"):
        assert store.save_flag(hull_id, {"status": "ok", "reviewer": "me"})["hull_id"] == hull_id
    scene = {"scene": "s", "site": "x", "source": "s2", "time": "2026-10-01T10:00:00Z"}
    store.save_checked(scene, [hull("a", "single"), hull("b")])
    assert sorted(f["hull_id"] for f in store.qa_flags()) == ["s__a", "s__gone"]
    kept = {h["hull_id"]: h["role"] for h in store.db.execute("select hull_id, role from review.hulls where run_key = %s", (store.run_key,))}
    assert kept == {"a": None, "b": None, "gone": "duplicate"}
    store.save_checked(scene, [hull("a"), hull("b")])
    assert store.db.execute("select count(*) as n from review.hulls where run_key = %s and role = 'duplicate'", (store.run_key,)).fetchone()["n"] == 1


def test_a_flag_is_stored_with_its_hull_site_and_read_back(store):
    add_scene(store, hulls=[hull("a", "pair", lat=25.5, lon=56.5)])
    row, error = runs.save_flag(store.run_no, "s__a", {"status": "wrong_verdict", "correct_verdict": "single", "note": "  tug  ", "reviewer": "dor"})
    assert error is None
    stored = pgdb_row(store, "s__a")
    assert (stored["scene_id"], stored["site"], stored["hull"], stored["source"], stored["verdict"]) == ("s", "x", "a", "s2", "pair")
    assert (stored["lat"], stored["lon"], stored["note"], stored["reviewer"], stored["correct_verdict"]) == (25.5, 56.5, "tug", "dor", "single")
    assert row["status"] == "wrong_verdict" and row["note"] == "tug" and row["updated_utc"] == iso(stored["updated_utc"])
    assert row["final_verdict"] == "single"
    again, _ = runs.save_flag(store.run_no, "s__a", {"status": "ok"})
    assert again["correct_verdict"] is None and again["final_verdict"] == "pair" and again["reviewer"] == "local"
    assert store.db.execute("select count(*) as n from review.qa_flags where run_key = %s", (store.run_key,)).fetchone()["n"] == 1


def pgdb_row(store, hull_id):
    return store.db.execute("select * from review.qa_flags where run_key = %s and hull_id = %s", (store.run_key, hull_id)).fetchone()


def test_flags_are_checked_before_they_are_stored(store):
    add_scene(store, hulls=[hull("a", "pair"), hull("u", "unmeasured"), hull("dup", "pair", role="duplicate")])
    assert runs.save_flag(store.run_no, "s__nope", {"status": "ok"})[1] == "unknown hull"
    assert runs.save_flag(store.run_no, "s__dup", {"status": "ok"})[1] == "unknown hull"
    assert "status must be one of" in runs.save_flag(store.run_no, "s__a", {"status": "is_pair"})[1]
    assert "status must be one of" in runs.save_flag(store.run_no, "s__u", {"status": "ok"})[1]
    assert "correct_verdict" in runs.save_flag(store.run_no, "s__a", {"status": "wrong_verdict", "correct_verdict": "pair"})[1]
    assert runs.save_flag(store.run_no, "s__u", {"status": "is_pair"})[1] is None
    assert store.db.execute("select count(*) as n from review.qa_flags where run_key = %s", (store.run_key,)).fetchone()["n"] == 1


def test_the_same_hull_name_in_two_sites_takes_the_site_suffix(store):
    add_scene(store, "s", "x", hulls=["a"])
    add_scene(store, "s", "y", hulls=["a"])
    assert store.flag_target("s__a") is None
    assert store.flag_target("s__a__x")["site"] == "x"
    assert store.save_flag("s__a__y", {"status": "ok"})["site"] == "y"


def test_flags_in_one_run_do_not_touch_the_same_hull_in_another(pgdb, tmp_path, monkeypatch):
    one, two = new_store(monkeypatch, tmp_path), new_store(monkeypatch, tmp_path)
    add_scene(one, hulls=["a"])
    add_scene(two, hulls=["a"])
    one.save_flag("s__a", {"status": "other"})
    assert [f["hull_id"] for f in one.qa_flags()] == ["s__a"] and two.qa_flags() == []
    assert two.final_verdicts() == {"s__a": "pair"} and one.final_verdicts() == {"s__a": "excluded"}


@pytest.mark.parametrize(
    "run_verdict, flag, expected",
    [
        ("pair", None, "pair"),
        ("moving", None, "single"),
        ("pair", ("ok", None), "pair"),
        ("single", ("ok", None), "single"),
        ("pair", ("wrong_verdict", "single"), "single"),
        ("single", ("wrong_verdict", "pair"), "pair"),
        ("single", ("is_pair", None), "pair"),
        ("unmeasured", ("is_single", None), "single"),
        ("pair", ("bad_geometry", None), "excluded"),
        ("pair", ("other", None), "excluded"),
    ],
)
def test_the_findings_view_applies_the_verdict_rules(store, run_verdict, flag, expected):
    add_scene(store, hulls=[hull("a", run_verdict)])
    if flag:
        status, correct = flag
        store.db.execute(
            "insert into review.qa_flags (run_key, hull_id, scene_id, site, hull, verdict, status, correct_verdict) "
            "values (%s, 's__a', 's', 'x', 'a', %s, %s, %s)",
            (store.run_key, "single" if run_verdict == "moving" else run_verdict, status, correct),
        )
    assert store.final_verdicts() == {"s__a": expected}


def test_the_findings_view_leaves_out_duplicates_and_second_hull_boxes(store):
    add_scene(store, hulls=[hull("a"), hull("d", role="duplicate"), hull("b", role="hull_b_box")])
    assert list(store.final_verdicts()) == ["s__a"]


def test_the_singles_sample_is_stable_and_always_keeps_flagged_hulls(store):
    names = [f"h{i:03d}" for i in range(60)]
    add_scene(store, hulls=[hull(n, "single") for n in names] + [hull("p", "pair")])
    index = runs.read_scene_index(store.run_key)
    assert runs.read_scene_index(store.run_key) == index
    assert index["review"]["s"] == [runs.SINGLES + 1, 0, 0]
    hulls = [{"id": f"s__{n}", "verdict": "single"} for n in names]
    _, first, _ = report.select_review_hulls(hulls, runs.SINGLES)
    assert report.select_review_hulls(list(reversed(hulls)), runs.SINGLES)[1] == first
    outside = next(n for n in names if f"s__{n}" not in first)
    store.save_flag(f"s__{outside}", {"status": "ok"})
    after = runs.read_scene_index(store.run_key)["review"]["s"]
    assert after == [runs.SINGLES + 1, 1, 0]
    _, kept, _ = report.select_review_hulls(hulls, runs.SINGLES, keep={f"s__{outside}"})
    assert f"s__{outside}" in kept and len(kept) == runs.SINGLES


def test_the_stamp_changes_when_a_hull_flag_or_ais_row_changes(store):
    add_scene(store, hulls=["a"])
    stamp = runs.stamp(store)
    assert runs.stamp(store) == stamp
    store.save_flag("s__a", {"status": "ok"})
    flagged = runs.stamp(store)
    assert flagged != stamp
    store.save_flag("s__a", {"status": "other"})
    assert runs.stamp(store) != flagged
    add_scene(store, "t", hulls=["b"])
    assert runs.stamp(store) != flagged


def test_the_run_listing_and_scene_rows_read_postgres(store):
    add_scene(store, hulls=[hull("a"), hull("b", "single")])
    store.save_flag("s__a", {"status": "ok"})
    runs_status = runs.summaries()[0]["status"]
    assert runs_status == "running"
    store.finish_run(1, "done")
    listed = runs.summaries()
    assert len(listed) == 1 and listed[0]["run"] == 1
    assert (listed[0]["scenes"], listed[0]["reviewed"], listed[0]["listed"], listed[0]["review"]) == (1, 1, 2, "in progress")
    assert listed[0]["verdicts"] == {"pair": 1, "single": 1} and listed[0]["captured"] == ["2026-10-01T10:00:00Z"] * 2
    rows = runs.scene_rows()
    assert [(r["run"], r["scene_id"], r["status"]) for r in rows] == [(1, "s", "done")]
    assert runs.summaries()[0]["status"] == "done"


def test_a_run_row_alone_lists_as_empty(store):
    assert runs.summaries() == [{"run": 1, "created": runs.summaries()[0]["created"], "area": None, "status": "empty"}]


def test_trace_lists_every_row_a_run_produced(pgdb, store):
    add_scene(store, hulls=["a", "b"])
    store.save_flag("s__a", {"status": "ok"})
    store.event(None, "info", None, "x")
    found = pg.trace(pgdb, run=store.run_no)
    pgdb.rollback()
    assert found["run_nos"] == [store.run_no]
    review = found["review"]
    assert (review["scenes"], review["hulls"], review["qa_flags"], review["events"], review["run_invocations"]) == (1, 2, 1, 1, 1)
    assert pg.check(pgdb) == []


def test_publishing_records_outputs_and_captures_with_their_hull(pgdb, store, tmp_path, monkeypatch):
    add_scene(store, hulls=["a"])
    root = tmp_path / "pub"
    (root / "sts" / "01-10-2026" / "x").mkdir(parents=True)
    (root / "captures" / "01-10-2026").mkdir(parents=True)
    table = root / "sts" / "01-10-2026" / "x" / "s2.parquet"
    table.write_bytes(b"parquet")
    png = root / "captures" / "01-10-2026" / "f.png"
    png.write_bytes(b"png")
    item = {"finding_id": "s2_20261001T100000_a", "files": {"png": png}, "run": store.run_no, "scene_id": "s", "site": "x", "hull_id": "a"}
    monkeypatch.setattr(publish, "out_root", lambda: root)
    monkeypatch.setattr(publish, "env", lambda name: "bucket")
    monkeypatch.setattr(publish, "settings", lambda: {"s3": {"findings_prefix": "sts/", "captures_prefix": "captures/"}})
    publish.record_and_upload({("01-10-2026", "x", "s2"): (table, [item])}, False, {})
    publish.record_and_upload({("01-10-2026", "x", "s2"): (table, [item])}, False, {})
    assert pgdb.execute("select scene_id, site, hull from review.captures").fetchall() == [("s", "x", "a")]
    assert pgdb.execute("select o.n_rows, s.scene_id from review.outputs o join review.output_scenes s using (run_key, path)").fetchall() == [(1, "s")]
    assert pg.check(pgdb) == []
