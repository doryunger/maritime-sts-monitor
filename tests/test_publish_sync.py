import io
from datetime import UTC, datetime

import fakeredis
import pyarrow.parquet as pq
import pytest
from PIL import Image

from maritime_tip_cue import publish, publish_merge, publish_sync, task_queue
from maritime_tip_cue.ais import match
from maritime_tip_cue.config import settings
from maritime_tip_cue.sts import rules as rules_mod
from tests.pgstore import add_scene, hull, new_store
from tests.test_publish_merge import S3

WHEN = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
KEY = settings()["s3"]["findings_prefix"] + "01-10-2026/x/s2.parquet"


class CountingS3(S3):
    def __init__(self):
        super().__init__()
        self.puts = 0

    def put_object(self, **kwargs):
        super().put_object(**kwargs)
        self.puts += 1


def prow(name, lat, qa="unreviewed", verdict="pair", run=1, job=None):
    return {
        "finding_id": f"s2_20261001T100000_{name}", "geometry": b"x", "lat": lat, "lon": 56.0, "capture_utc": WHEN, "source": "s2",
        "hot_spot": "x", "scene_id": "s", "hull_id": name, "run": run, "job_id": job, "measures": '{"hull_a_length_m": 200.0}',
        "qa_status": qa, "verdict": verdict, "also_in": None, "qa_note": None, "capture_png": f"cap/{name}.png", "attempt_id": "att-1",
    }


def stored(s3):
    return {r["finding_id"].split("_")[-1]: r for r in pq.read_table(io.BytesIO(s3.objects[KEY])).to_pylist()}


class World:
    def __init__(self, monkeypatch, tmp_path):
        self.s3 = CountingS3()
        self.matched = []
        self.store = new_store(monkeypatch, tmp_path)
        invocation = self.store.start_run({}, "x", ("a", "b"), "s2", "r", "c")
        self.store.finish_run(invocation, "done")
        self.rules = rules_mod.load()

    def hulls(self, specs):
        add_scene(self.store, "s", "x", "s2", "2026-10-01T10:00:00Z", [hull(name, verdict, lat=lat, lon=56.0) for name, verdict, lat in specs])

    def flag(self, name, status, correct=None):
        self.store.db.execute(
            "insert into review.qa_flags (run_key, hull_id, scene_id, status, correct_verdict, site, hull, verdict) values (%s,%s,'s',%s,%s,'x',%s,%s)",
            (self.store.run_key, f"s__{name}", status, correct, name, "pair" if status == "ok" else None),
        )

    def publish(self, rows):
        key_rows = publish_merge.encode_rows(rows)
        self.s3.objects[KEY] = key_rows
        self.s3.etags[KEY] = '"seed"'

    def sync(self, upload=True, dry_run=False, runs=(1,)):
        return publish_sync.sync(self.s3, "b", list(runs), upload, dry_run, self.rules)


@pytest.fixture
def world(pgdb, monkeypatch, tmp_path):
    monkeypatch.setattr(task_queue, "client", lambda: fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(publish, "out_root", lambda: tmp_path / "pub")
    monkeypatch.setattr(match, "refresh_identities", lambda store: None)
    monkeypatch.setattr("maritime_tip_cue.equasis.resolve.enrich", lambda store, rule: {})
    monkeypatch.setattr("maritime_tip_cue.sanctions.apply", lambda store: 0)
    monkeypatch.setattr(publish.report, "render", lambda *a, **k: Image.new("RGB", (8, 8)))
    monkeypatch.setattr(publish.report, "vessel_points", lambda *a, **k: [])
    monkeypatch.setattr(publish.report, "window_of", lambda *a, **k: None)
    w = World(monkeypatch, tmp_path)
    monkeypatch.setattr(match, "run_match", lambda store, rows, rules, log=print: w.matched.append(sorted(r["hull_id"] for r in rows)) or {})
    return w


def test_pair_turned_single_is_kept_as_rejected_with_its_captures(world):
    world.hulls([("a", "pair", 25.0), ("b", "pair", 25.1)])
    world.flag("a", "is_single")
    world.publish([prow("a", 25.0), prow("b", 25.1)])
    lines = world.sync()
    rows = stored(world.s3)
    assert rows["a"]["qa_status"] == "rejected" and rows["a"]["verdict"] == "single" and rows["a"]["capture_png"] == "cap/a.png"
    assert rows["b"]["qa_status"] == "unreviewed" and rows["b"]["verdict"] == "pair"
    assert any("rejected 1" in line and "unchanged 1" in line for line in lines)


def test_excluded_review_keeps_the_row_as_rejected_excluded(world):
    world.hulls([("a", "pair", 25.0)])
    world.flag("a", "bad_geometry")
    world.publish([prow("a", 25.0)])
    world.sync()
    assert (stored(world.s3)["a"]["qa_status"], stored(world.s3)["a"]["verdict"]) == ("rejected", "excluded")


def test_rejected_row_returns_when_review_makes_it_a_pair_again(world):
    world.hulls([("a", "single", 25.0), ("b", "pair", 25.1)])
    world.flag("a", "is_pair")
    world.flag("b", "ok")
    world.publish([prow("a", 25.0, qa="rejected", verdict="single"), prow("b", 25.1, qa="rejected", verdict="single")])
    world.sync()
    rows = stored(world.s3)
    assert (rows["a"]["qa_status"], rows["a"]["verdict"]) == ("corrected", "pair")
    assert (rows["b"]["qa_status"], rows["b"]["verdict"]) == ("confirmed", "pair")


def test_confirmed_and_corrected_pairs_update_their_status_and_unflagged_stay(world):
    world.hulls([("a", "pair", 25.0), ("b", "pair", 25.1), ("c", "pair", 25.2), ("d", "pair", 25.3)])
    world.flag("a", "ok")
    world.flag("b", "is_pair")
    world.flag("c", "wrong_verdict", "pair")
    world.publish([prow(n, 25.0 + i / 10) for i, n in enumerate("abcd")])
    lines = world.sync()
    assert {n: r["qa_status"] for n, r in stored(world.s3).items()} == {"a": "confirmed", "b": "corrected", "c": "corrected", "d": "unreviewed"}
    assert any("status_updated 3" in line and "unchanged 1" in line for line in lines)


def test_single_turned_pair_is_added_with_ais_for_that_hull_only_and_dedupe_applied(world, tmp_path):
    world.hulls([("a", "pair", 25.0), ("new", "single", 25.5), ("twin", "single", 25.0001)])
    world.flag("new", "is_pair")
    world.flag("twin", "is_pair")
    world.publish([prow("a", 25.0)])
    lines = world.sync()
    assert world.matched == [["new"]]
    rows = stored(world.s3)
    assert sorted(rows) == ["a", "new"] and rows["new"]["verdict"] == "pair" and rows["new"]["qa_status"] == "corrected"
    assert rows["new"]["attempt_id"] == "att-1" and rows["new"]["capture_png"].endswith(".png")
    assert any("added 1" in line for line in lines) and "1 pairs skipped as duplicates" in lines[-1]
    assert len([k for k in world.s3.uploads if k.endswith(".png")]) == 1
    fetched = world.store.db.execute("select scene_id, site, hull, uploaded_utc from review.captures where run_key = %s", (world.store.run_key,)).fetchall()
    assert [(r["scene_id"], r["site"], r["hull"]) for r in fetched] == [("s", "x", "new")] and fetched[0]["uploaded_utc"] is not None
    out = world.store.db.execute("select site, source, n_rows from review.outputs where run_key = %s", (world.store.run_key,)).fetchall()
    assert [(r["site"], r["source"], r["n_rows"]) for r in out] == [("x", "s2", 2)]
    assert world.store.db.execute("select scene_id from review.output_scenes where run_key = %s", (world.store.run_key,)).fetchall()[0]["scene_id"] == "s"


def test_second_sync_changes_nothing_and_says_so(world):
    world.hulls([("a", "pair", 25.0), ("b", "pair", 25.1), ("new", "single", 25.5)])
    world.flag("a", "is_single")
    world.flag("b", "ok")
    world.flag("new", "is_pair")
    world.publish([prow("a", 25.0), prow("b", 25.1)])
    world.sync()
    puts, uploads, matched = world.s3.puts, len(world.s3.uploads), len(world.matched)
    lines = world.sync()
    assert lines[-1].startswith("nothing to change")
    assert (world.s3.puts, len(world.s3.uploads), len(world.matched)) == (puts, uploads, matched)


def test_rows_of_other_jobs_in_the_same_file_are_untouched(world):
    world.hulls([("a", "pair", 25.0)])
    world.flag("a", "is_single")
    other = prow("z", 26.0, run=99, job="OTHER")
    world.publish([prow("a", 25.0), other])
    world.sync()
    rows = stored(world.s3)
    assert rows["z"]["run"] == 99 and rows["z"]["qa_status"] == "unreviewed" and rows["a"]["qa_status"] == "rejected"


def test_dry_run_and_local_mode_leave_s3_and_the_database_alone(world, tmp_path):
    world.hulls([("a", "pair", 25.0), ("new", "single", 25.5)])
    world.flag("a", "is_single")
    world.flag("new", "is_pair")
    world.publish([prow("a", 25.0)])
    before = world.s3.objects[KEY]
    lines = world.sync(dry_run=True, upload=True)
    assert "dry run" in lines[-1] and "rejected 1" in lines[0] and "added 1" in lines[0]
    assert world.matched == [] and world.s3.puts == 0 and not world.s3.uploads and not (tmp_path / "pub").exists()
    world.sync(upload=False)
    assert world.s3.objects[KEY] == before and world.s3.puts == 0 and not world.s3.uploads
    assert world.store.db.execute("select count(*) as n from review.outputs").fetchone()["n"] == 0
    assert list((tmp_path / "pub" / "sts").rglob("*.parquet"))


def test_etag_conflicts_are_retried(world):
    world.hulls([("a", "pair", 25.0)])
    world.flag("a", "ok")
    world.publish([prow("a", 25.0)])
    world.s3.conflicts = 2
    world.sync()
    assert stored(world.s3)["a"]["qa_status"] == "confirmed"


def test_rejected_twin_does_not_block_a_new_pair_of_the_same_vessel(world):
    world.hulls([("new", "single", 25.0)])
    world.flag("new", "is_pair")
    world.publish([prow("old", 25.0, qa="rejected", verdict="single", run=99)])
    world.sync()
    assert "new" in stored(world.s3)


def test_explicit_run_that_is_not_done_refuses_everything(world):
    world.store.db.execute("update review.run_invocations set status = 'failed' where run_key = %s", (world.store.run_key,))
    with pytest.raises(SystemExit, match="run status is failed"):
        world.sync()


def test_rows_published_under_an_old_run_number_are_still_matched_by_scene_and_hull(world):
    world.hulls([("a", "pair", 25.0), ("b", "pair", 25.1)])
    world.flag("a", "is_single")
    world.publish([prow("a", 25.0, run=7), prow("b", 25.1, run=7)])
    lines = world.sync()
    rows = stored(world.s3)
    assert rows["a"]["qa_status"] == "rejected" and rows["b"]["qa_status"] == "unreviewed"
    assert any("rejected 1" in line and "unchanged 1" in line for line in lines)


def held(run_key, folder, attempt=None, job=None):
    return {"run_key": run_key, "folder": folder, "attempt_id": attempt, "job_id": job}


def test_a_published_row_goes_to_the_run_its_attempt_or_job_or_legacy_folder_names():
    both = [held(1, "j1-a1", "att-1", "job-1"), held(2, "j1-a2", "att-2", "job-1")]
    assert publish_sync.pick_owner({"attempt_id": "att-2", "job_id": "job-1", "run": 9}, both) == 2
    assert publish_sync.pick_owner({"attempt_id": None, "job_id": "job-1", "run": 9}, both) is None
    one_job = [held(1, "j1-a1", "att-1", "job-1"), held(2, "j2-a1", "att-9", "job-2")]
    assert publish_sync.pick_owner({"attempt_id": None, "job_id": "job-2", "run": 9}, one_job) == 2
    legacy = [held(5, "legacy-run-3"), held(6, "legacy-run-4"), held(7, "j9-a9", "att-9", "job-9")]
    assert publish_sync.pick_owner({"attempt_id": None, "job_id": None, "run": 4}, legacy) == 6
    assert publish_sync.pick_owner({"attempt_id": None, "job_id": None, "run": 8}, legacy) is None
    assert publish_sync.pick_owner({"attempt_id": "x", "job_id": "y", "run": 1}, [held(3, "f")]) == 3
    assert publish_sync.pick_owner({"attempt_id": None, "job_id": None, "run": 1}, []) is None

