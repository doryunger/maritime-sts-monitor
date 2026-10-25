import hashlib
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.backend import s3runs
from maritime_tip_cue.watch import registry

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


JOB = "j" * 64
ATTEMPT = "a" * 32


@pytest.fixture
def env(pgdb, monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr(s3runs, "settings", lambda: {"catalogue": {"reviews_prefix": PREFIX}})
    s3runs._missing.clear()
    return S3()


def job_run(conn, job_id=JOB, attempt_id=ATTEMPT):
    conn.execute(
        "insert into processing.jobs (job_id, site, source, created_utc, manifest_json, manifest_sha256, status) "
        "values (%s, 'x', 's2', now(), '{}', 'x', 'succeeded') on conflict do nothing",
        (job_id,),
    )
    conn.execute(
        "insert into processing.job_attempts (attempt_id, job_id, attempt_no, owner, status, started_utc) "
        "values (%s, %s, 1, 'w', 'succeeded', now())",
        (attempt_id, job_id),
    )
    conn.commit()
    return registry.new_run("job", attempt_id, job_id)


def review_folder(tmp_path, name="src"):
    folder = tmp_path / name
    (folder / "scenes").mkdir(parents=True)
    (folder / "scenes" / "w.npz").write_bytes(b"npz")
    return folder


def upload(s3, folder, prefix, tamper=False):
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            s3.put(prefix + path.relative_to(folder).as_posix(), data)
            files.append({"path": path.relative_to(folder).as_posix(), "size": len(data) + (1 if tamper else 0), "sha256": hashlib.sha256(data).hexdigest()})
    s3.put(prefix + "index.json", json.dumps({"files": files}).encode())


def test_job_run_is_downloaded_without_a_database_file_and_not_repeated(env, pgdb, tmp_path):
    run = job_run(pgdb)
    upload(env, review_folder(tmp_path), f"{PREFIX}{JOB}/{ATTEMPT}/")
    first = s3runs.sync_once(env, "b")
    assert first == {"added": [run["folder"]], "errors": []}
    assert run["folder"] == s3runs.folder_name(JOB, ATTEMPT)
    assert (registry.runs_root() / run["folder"] / "scenes" / "w.npz").is_file()
    assert not list((registry.runs_root() / run["folder"]).glob("state.db*"))
    assert s3runs.sync_once(env, "b")["added"] == []
    assert len(registry.listing()) == 1


def test_a_run_with_a_local_folder_is_left_alone_and_never_replaced(env, pgdb, tmp_path):
    run = job_run(pgdb)
    local = registry.runs_root() / run["folder"]
    local.mkdir(parents=True)
    (local / "mine.txt").write_bytes(b"keep")
    upload(env, review_folder(tmp_path), f"{PREFIX}{JOB}/{ATTEMPT}/")
    assert s3runs.sync_once(env, "b") == {"added": [], "errors": []}
    assert (local / "mine.txt").read_bytes() == b"keep" and not (local / "scenes").exists()


def test_legacy_runs_are_loaded_from_their_own_prefix(env, tmp_path):
    run = registry.new_run("legacy")
    upload(env, review_folder(tmp_path), f"{PREFIX}legacy/run-{run['run_no']}/")
    assert s3runs.sync_once(env, "b")["added"] == [f"legacy-run-{run['run_no']}"]


def test_a_job_run_is_fetched_under_its_job_and_attempt_not_a_guess(env, pgdb, tmp_path):
    job_run(pgdb)
    upload(env, review_folder(tmp_path), f"{PREFIX}{'k' * 64}/{ATTEMPT}/")
    assert len(s3runs.sync_once(env, "b")["errors"]) == 1


def test_corrupt_download_is_discarded_and_retried_later(env, pgdb, tmp_path):
    job_run(pgdb)
    upload(env, review_folder(tmp_path), f"{PREFIX}{JOB}/{ATTEMPT}/", tamper=True)
    result = s3runs.sync_once(env, "b")
    assert result["added"] == [] and "checksum" in result["errors"][0]["error"]
    assert not list((tmp_path / "runs").glob(".tmp-*")) and not list((tmp_path / "runs").glob("[!.]*"))


def test_run_without_review_data_is_remembered_not_retried_every_cycle(env, pgdb):
    job_run(pgdb)
    first = s3runs.sync_once(env, "b")
    assert len(first["errors"]) == 1
    assert s3runs.sync_once(env, "b")["errors"] == []


def test_unsafe_index_paths_are_rejected(env, pgdb):
    job_run(pgdb)
    env.put(f"{PREFIX}{JOB}/{ATTEMPT}/index.json", json.dumps({"files": [{"path": "../x", "size": 1, "sha256": "0"}]}).encode())
    assert "unsafe path" in s3runs.sync_once(env, "b")["errors"][0]["error"]
