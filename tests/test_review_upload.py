import json

from maritime_tip_cue import worker


class Client:
    def __init__(self):
        self.files = {}
        self.objects = {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.files[key] = ExtraArgs["Metadata"]["sha256"]

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = json.loads(Body)


def test_review_folder_is_uploaded_with_an_index_and_without_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "settings", lambda: {"catalogue": {"reviews_prefix": "catalogue/reviews/"}})
    out = tmp_path / "run"
    (out / "scenes" / "s1").mkdir(parents=True)
    (out / "cache").mkdir()
    (out / "review.png").write_bytes(b"png")
    (out / "scenes" / "s1" / "a.npz").write_bytes(b"npz")
    (out / "cache" / "x_card.png").write_bytes(b"cache")
    (out / "state.db").write_bytes(b"live")
    (out / "state.db-wal").write_bytes(b"wal")
    snapshot = tmp_path / "snap.db"
    snapshot.write_bytes(b"snap")
    client = Client()
    summary = worker.upload_review(client, "b", {"job_id": "J"}, out, snapshot, 7, "A")
    prefix = "catalogue/reviews/J/A/"
    assert sorted(client.files) == [prefix + "review.png", prefix + "scenes/s1/a.npz", prefix + "state.db"]
    assert summary == {"prefix": prefix, "index": prefix + "index.json", "files": 3}
    index = client.objects[prefix + "index.json"]
    assert index["run_no"] == 7 and {f["path"] for f in index["files"]} == {"review.png", "scenes/s1/a.npz", "state.db"}


def test_review_upload_failure_is_recorded_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "upload_review", lambda *a: (_ for _ in ()).throw(PermissionError("AccessDenied")))
    receipt = {"status": "succeeded"}
    worker.attach_review(receipt, None, "b", {}, tmp_path, tmp_path, 1, "A")
    assert receipt["review"] is None and "AccessDenied" in receipt["review_error"]


def test_progress_is_read_from_the_run_database(tmp_path, monkeypatch):
    from maritime_tip_cue.watch.state import Store

    store = Store(tmp_path / "state.db", tmp_path)
    store.db.execute(
        "insert into scenes (scene_id, site, source, status, stage, updated_utc) values ('s1','x','s2','running','detect','t')"
    )
    store.db.commit()
    store.close()
    saved = {}
    monkeypatch.setattr(worker.task_queue, "save_progress", lambda job_id, scenes: saved.update(job=job_id, scenes=scenes))
    worker.ACTIVE.clear()
    worker.publish_progress("J")
    assert saved == {}
    worker.ACTIVE.update(out=tmp_path)
    worker.publish_progress("J")
    worker.ACTIVE.clear()
    assert saved["job"] == "J" and saved["scenes"]["s1"]["status"] == "running" and saved["scenes"]["s1"]["stage"] == "detect"


def test_provenance_ties_a_finding_to_its_attempt_review_rules_and_code(monkeypatch):
    monkeypatch.setenv("STS_GIT_COMMIT", "abc123")
    monkeypatch.setenv("STS_DETECTOR_REVISION", "det456")
    result = worker.provenance_of({"rules_sha256": "r" * 64}, "A1", {"prefix": "catalogue/reviews/J/A1/"})
    assert result == {
        "attempt_id": "A1",
        "review_prefix": "catalogue/reviews/J/A1/",
        "rules_sha256": "r" * 64,
        "code_commit": "abc123",
        "detector_revision": "det456",
    }
    assert worker.provenance_of({"rules_sha256": "r"}, "A1", None)["review_prefix"] is None
