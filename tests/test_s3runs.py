import hashlib
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.backend import s3runs
from maritime_tip_cue.watch import registry
from maritime_tip_cue.watch.state import Store

PREFIX = "catalogue/reviews/"


class S3:
    def __init__(self):
        self.objects = {}

    def put(self, key, data):
        self.objects[key] = data

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, bucket, key, destination):
        Path(destination).write_bytes(self.objects[key])

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        seen = sorted({k[len(Prefix):].split("/")[0] for k in self.objects if k.startswith(Prefix) and "/" in k[len(Prefix):]})
        return {"CommonPrefixes": [{"Prefix": f"{Prefix}{name}/"} for name in seen], "IsTruncated": False}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr(s3runs, "settings", lambda: {"catalogue": {"reviews_prefix": PREFIX}})
    s3runs._missing.clear()
    return S3()


def run_folder(tmp_path, name="src"):
    folder = tmp_path / name
    folder.mkdir()
    store = Store(folder / "state.db", folder)
    run_id = store.start_run({"job_id": "J"}, "site", ("a", "b"), "s2", "r", "c")
    store.finish_run(run_id, "done")
    store.close()
    (folder / "scenes").mkdir()
    (folder / "scenes" / "w.npz").write_bytes(b"npz")
    return folder


def upload(s3, folder, prefix, tamper=False):
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name == "state.db" or path.suffix == ".npz":
            data = path.read_bytes()
            s3.put(prefix + path.relative_to(folder).as_posix(), data)
            files.append({"path": path.relative_to(folder).as_posix(), "size": len(data) + (1 if tamper else 0), "sha256": hashlib.sha256(data).hexdigest()})
    s3.put(prefix + "index.json", json.dumps({"files": files}).encode())


JOBS = [{"job_id": "j" * 64, "status": "succeeded", "attempts": [{"attempt_id": "a" * 32, "verdict": "succeeded"}]}]


def test_succeeded_attempt_is_downloaded_registered_and_not_repeated(env, tmp_path):
    upload(env, run_folder(tmp_path), f"{PREFIX}{'j' * 64}/{'a' * 32}/")
    first = s3runs.sync_once(env, "b", JOBS)
    assert first["added"] == [s3runs.folder_name("j" * 64, "a" * 32)] and first["errors"] == []
    listing = registry.listing()
    assert len(listing) == 1 and (registry.runs_root() / listing[0]["folder"] / "scenes" / "w.npz").is_file()
    assert s3runs.sync_once(env, "b", JOBS)["added"] == []
    assert len(registry.listing()) == 1


def test_legacy_runs_are_loaded_from_their_own_prefix(env, tmp_path):
    upload(env, run_folder(tmp_path), f"{PREFIX}legacy/run-3/")
    result = s3runs.sync_once(env, "b", [])
    assert result["added"] == ["legacy-run-3"]


def test_failed_or_unfinished_jobs_are_not_loaded(env, tmp_path):
    jobs = [dict(JOBS[0], status="failed"), dict(JOBS[0], job_id="k" * 64, attempts=[{"attempt_id": "b" * 32, "verdict": "failed"}])]
    assert s3runs.sync_once(env, "b", jobs) == {"added": [], "errors": []}


def test_corrupt_download_is_discarded_and_retried_later(env, tmp_path):
    upload(env, run_folder(tmp_path), f"{PREFIX}{'j' * 64}/{'a' * 32}/", tamper=True)
    result = s3runs.sync_once(env, "b", JOBS)
    assert result["added"] == [] and "checksum" in result["errors"][0]["error"]
    assert registry.listing() == [] and not list((tmp_path / "runs").glob(".tmp-*"))


def test_attempt_without_review_data_is_remembered_not_retried_every_cycle(env):
    first = s3runs.sync_once(env, "b", JOBS)
    assert len(first["errors"]) == 1
    assert s3runs.sync_once(env, "b", JOBS)["errors"] == []


def test_unsafe_index_paths_are_rejected(env, tmp_path):
    env.put(f"{PREFIX}{'j' * 64}/{'a' * 32}/index.json", json.dumps({"files": [{"path": "../x", "size": 1, "sha256": "0"}]}).encode())
    assert "unsafe path" in s3runs.sync_once(env, "b", JOBS)["errors"][0]["error"]
