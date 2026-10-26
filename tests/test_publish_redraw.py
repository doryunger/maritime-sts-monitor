import io
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from maritime_tip_cue import publish, publish_redraw
from tests.test_publish_merge import S3

KEY = "sts/21-10-2026/fujairah_outer/s2.parquet"
PNG = "captures/21-10-2026/fujairah_outer/s2/s2_20261021T065200_h1_KRONOS.png"


def row(finding_id, hull, status="confirmed", png=PNG, run=3):
    return {
        "finding_id": finding_id, "run": run, "scene_id": "S", "hot_spot": "fujairah_outer", "hull_id": hull, "qa_status": status,
        "capture_png": png, "capture_utc": datetime(2026, 10, 21, 6, 52, tzinfo=UTC),
    }


def store_rows(s3, rows):
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    s3.objects[KEY] = sink.getvalue()
    s3.etags[KEY] = '"1"'


class Put(S3):
    def __init__(self):
        super().__init__()
        self.puts = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, **kwargs):
        if Key.startswith("captures/"):
            self.puts[Key] = (Body, ContentType)
            return
        super().put_object(Bucket, Key, Body, ContentType, **kwargs)


def found(hull):
    return {"scene_id": "S", "site": "fujairah_outer", "hull_id": hull}


def draw(f, base, rules, root, single=False):
    path = root / f"{base}.png"
    path.write_bytes(b"new")
    return {"png": path}


def test_redraw_replaces_only_the_image_at_the_existing_key_and_leaves_the_parquet_alone(tmp_path, monkeypatch):
    s3 = Put()
    store_rows(s3, [row("a", "h1"), row("b", "h2", status="rejected", png="captures/x.png")])
    s3.objects[PNG] = b"old"
    s3.etags[PNG] = '"2"'
    before = s3.objects[KEY]
    asked = []

    def collect(path, no, job_id, rules, only=None, verdicts=("pair",), enrich=True):
        asked.append((no, sorted(only), enrich))
        return [found("h1")]

    monkeypatch.setattr(publish, "collect_at", collect)
    monkeypatch.setattr(publish, "complete", lambda items: items)
    monkeypatch.setattr(publish, "write_captures", draw)
    rows = publish_redraw.published_rows(s3, "b")
    backup = tmp_path / "backup"
    summary = publish_redraw.redraw(s3, "b", rows, {}, lambda no: tmp_path, True, backup, lambda *a: None, runs_for=lambda row: [row["run"]])
    assert asked == [(3, [("S", "fujairah_outer", "h1")], False)]
    assert s3.puts == {PNG: (b"new", "image/png")}
    assert (backup / PNG).read_bytes() == b"old" and s3.objects[KEY] == before
    assert (summary["rows"], summary["redrawn"], summary["uploaded"], summary["not_found"]) == (1, 1, 1, [])


def test_a_dry_run_draws_but_uploads_nothing_and_reports_rows_it_could_not_redraw(tmp_path, monkeypatch):
    s3 = Put()
    store_rows(s3, [row("a", "h1"), row("c", "h3", png="captures/c.png")])
    monkeypatch.setattr(publish, "collect_at", lambda *a, **k: [found("h1")])
    monkeypatch.setattr(publish, "complete", lambda items: items)
    monkeypatch.setattr(publish, "write_captures", draw)
    summary = publish_redraw.redraw(s3, "b", publish_redraw.published_rows(s3, "b"), {}, lambda no: tmp_path, False, None, lambda *a: None, runs_for=lambda row: [row["run"]])
    assert s3.puts == {} and summary["redrawn"] == 1 and summary["not_found"] == ["c"] and summary["uploaded"] == 0


def test_a_row_is_tried_against_each_run_of_its_job_until_the_hull_is_found(tmp_path, monkeypatch):
    s3 = Put()
    store_rows(s3, [row("a", "h1")])
    asked = []

    def collect(path, no, job_id, rules, only=None, verdicts=("pair",), enrich=True):
        asked.append(no)
        return [found("h1")] if no == 7 else []

    monkeypatch.setattr(publish, "collect_at", collect)
    monkeypatch.setattr(publish, "complete", lambda items: items)
    monkeypatch.setattr(publish, "write_captures", draw)
    summary = publish_redraw.redraw(s3, "b", publish_redraw.published_rows(s3, "b"), {}, lambda no: tmp_path, False, None, lambda *a: None, runs_for=lambda row: [9, 7, 5])
    assert asked == [5, 7] and summary["redrawn"] == 1 and summary["not_found"] == []


def test_the_run_of_a_published_row_is_found_by_its_hull_among_runs_where_it_is_still_a_pair(pgdb, tmp_path, monkeypatch):
    from tests.pgstore import add_scene, hull, new_store

    first = new_store(monkeypatch, tmp_path / "a")
    add_scene(first, scene_id="S", site="x", hulls=[hull("p", "pair"), hull("q", "single")])
    second = new_store(monkeypatch, tmp_path / "b")
    add_scene(second, scene_id="S", site="x", hulls=[hull("p", "pair")])
    both = sorted([first.run_no, second.run_no])
    row = {"scene_id": "S", "hot_spot": "x", "hull_id": "p", "run": 999}
    assert publish_redraw.runs_of(row) == both
    assert publish_redraw.runs_of(row | {"run": second.run_no}) == [second.run_no]
    assert publish_redraw.runs_of(row | {"hull_id": "q"}) == []
    first.close()
    second.close()
