import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from maritime_tip_cue import task_queue
from maritime_tip_cue.config import ROOT, env, settings


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def worker_root() -> Path:
    return ROOT / settings()["worker"]["path"]


@contextmanager
def local_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "worker.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def json_object(client, bucket: str, key: str) -> tuple[dict | None, str | None]:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return None, None
        raise
    return json.loads(response["Body"].read()), response.get("ETag")


def key_for(kind: str, job_id: str) -> str:
    return settings()["catalogue"][f"{kind}_prefix"].rstrip("/") + "/" + job_id + ".json"


def validate(manifest: dict) -> None:
    from maritime_tip_cue.catalogue import validate_manifest

    validate_manifest(manifest)
    for row in manifest["scenes"]:
        scene = row["scene"]
        if (
            not isinstance(scene, str)
            or not scene
            or scene in (".", "..")
            or any(c in scene for c in "/\\:\x00")
            or Path(scene).name != scene
        ):
            raise ValueError("scene identifier is not a safe filename")
        if row["source"] not in ("s1", "s2") or row["site"] != manifest["site"]["name"]:
            raise ValueError("scene does not match job site or supported source")
    if not manifest["scenes"]:
        raise ValueError("empty job manifest")
    if len({(row["site"], row["source"], row["scene"]) for row in manifest["scenes"]}) != len(manifest["scenes"]):
        raise ValueError("duplicate scene in job manifest")


def save_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encode(value))
    temporary.replace(path)


def claim(client, bucket: str, manifest: dict, workspace: Path, resume: bool) -> dict:
    path = workspace / "owner.json"
    if path.exists():
        owner = json.loads(path.read_bytes())
    else:
        owner = {
            "schema_version": 1,
            "job_id": manifest["job_id"],
            "owner_token": uuid.uuid4().hex,
            "created_utc": now(),
        }
        save_json(path, owner)
    key = key_for("claims", manifest["job_id"])
    try:
        client.put_object(Bucket=bucket, Key=key, Body=encode(owner), ContentType="application/json", IfNoneMatch="*")
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
            raise
        existing, _ = json_object(client, bucket, key)
        if not resume or existing != owner:
            raise RuntimeError("job already claimed; --resume requires the original worker workspace") from error
    return owner


def export_parquets(run_no: int, out: Path, destination: Path, job_id: str) -> list[Path]:
    import pyarrow as pa
    import pyarrow.parquet as pq
    from shapely.geometry import Point

    from maritime_tip_cue.watch.state import Store

    destination.mkdir(parents=True, exist_ok=True)
    store = Store(out / "state.db", out)
    try:
        rows = []
        query = (
            "select h.*, s.source, s.capture_utc, f.run_verdict, f.verdict as final_verdict, f.qa_status, f.qa_note "
            "from hulls h join scenes s using(scene_id,site) "
            "left join findings f using(scene_id,site,hull_id) order by h.site,h.scene_id,h.hull_id"
        )
        for value in store.db.execute(query):
            row = dict(value)
            key = (row["scene_id"], row["site"], row["hull_id"])
            matches = [
                dict(item)
                for item in store.db.execute("select * from ais_matches where scene_id=? and site=? and hull_id=?", key)
            ]
            coverage = store.db.execute("select * from ais_coverage where scene_id=? and site=?", key[:2]).fetchone()
            row.update(
                job_id=job_id,
                run_no=run_no,
                ais_matches=json.dumps(matches) if matches else None,
                ais_coverage=json.dumps(dict(coverage)) if coverage else None,
            )
            row["geometry"] = (
                Point(row["lon"], row["lat"]).wkb if row["lat"] is not None and row["lon"] is not None else None
            )
            rows.append(row)
        fields = [
            ("scene_id", pa.string()),
            ("site", pa.string()),
            ("hull_id", pa.string()),
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("ai2_length_m", pa.float64()),
            ("ai2_width_m", pa.float64()),
            ("ai2_heading", pa.float64()),
            ("verdict", pa.string()),
            ("role", pa.string()),
            ("record", pa.string()),
            ("source", pa.string()),
            ("capture_utc", pa.string()),
            ("run_verdict", pa.string()),
            ("final_verdict", pa.string()),
            ("qa_status", pa.string()),
            ("qa_note", pa.string()),
            ("job_id", pa.string()),
            ("run_no", pa.int64()),
            ("ais_matches", pa.string()),
            ("ais_coverage", pa.string()),
            ("geometry", pa.binary()),
        ]
        table = pa.Table.from_pylist(rows, schema=pa.schema(fields))
        geo = {
            "version": "1.0.0",
            "primary_column": "geometry",
            "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Point"]}},
        }
        table = table.replace_schema_metadata({b"geo": encode(geo)})
        path = destination / "detections.parquet"
        pq.write_table(table, path)
        raw = [
            dict(row) | {"job_id": job_id, "run_no": run_no}
            for row in store.db.execute("select * from raw_detections order by site,scene_id,idx")
        ]
        raw_schema = pa.schema(
            [
                ("scene_id", pa.string()),
                ("site", pa.string()),
                ("idx", pa.int64()),
                ("col", pa.int64()),
                ("row", pa.int64()),
                ("lat", pa.float64()),
                ("lon", pa.float64()),
                ("score", pa.float64()),
                ("feature", pa.string()),
                ("job_id", pa.string()),
                ("run_no", pa.int64()),
            ]
        )
        raw_path = destination / "raw_detections.parquet"
        pq.write_table(pa.Table.from_pylist(raw, schema=raw_schema), raw_path)
        snapshot = destination / "state.db"
        target = sqlite3.connect(snapshot)
        try:
            store.db.backup(target)
        finally:
            target.close()
        return [path, raw_path, snapshot]
    finally:
        store.close()


def upload_artifacts(client, bucket: str, manifest: dict, paths: list[Path], attempt: str) -> list[dict]:
    prefix = settings()["catalogue"]["artifacts_prefix"].rstrip("/") + f"/{manifest['job_id']}/{attempt}/"
    artifacts = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = prefix + path.name
        client.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}})
        artifacts.append({"key": key, "sha256": digest, "size": path.stat().st_size, "kind": path.name})
    return artifacts


def run_job(client, bucket: str, manifest: dict, resume: bool = False, queue_owner: str | None = None) -> dict:
    validate(manifest)
    root = worker_root()
    with local_lock(root):
        job_id = manifest["job_id"]
        result_key = key_for("results", job_id)
        previous, result_etag = json_object(client, bucket, result_key)
        if previous and previous["status"] == "succeeded":
            if previous.get("job_id") != job_id or previous.get("manifest_sha256") != job_id:
                raise RuntimeError("existing receipt does not match manifest")
            return previous
        workspace = root / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        if queue_owner is None:
            claim(client, bucket, manifest, workspace, resume)
        save_json(workspace / "manifest.json", manifest)
        rules_path = workspace / "rules.json"
        save_json(rules_path, manifest["rules"])
        receipt = {
            "schema_version": 1,
            "job_id": job_id,
            "manifest_sha256": job_id,
            "status": "failed",
            "run_no": None,
            "started_utc": now(),
            "finished_utc": None,
            "artifacts": [],
            "scene_counts": {"checked": 0, "failed": len(manifest["scenes"])},
            "error": None,
        }
        try:
            from maritime_tip_cue.watch import jobs, registry

            pointer = workspace / "run.json"
            if pointer.exists():
                run_no = json.loads(pointer.read_bytes())["run_no"]
                out = registry.folder_of(run_no)
            else:
                run_no, out = registry.new_run()
                save_json(pointer, {"run_no": run_no})
            receipt["run_no"] = run_no
            receipt["scene_counts"] = jobs.execute(manifest, run_no, out, rules_path)
            attempt = uuid.uuid4().hex
            paths = export_parquets(run_no, out, workspace / attempt, job_id)
            paths += [workspace / "manifest.json", rules_path]
            receipt["artifacts"] = upload_artifacts(client, bucket, manifest, paths, attempt)
            receipt["status"] = "succeeded"
        except Exception as error:
            receipt["error"] = f"{type(error).__name__}: {error}"
            if receipt["run_no"] is not None:
                from maritime_tip_cue.watch.state import Store

                store = Store(out / "state.db", out)
                try:
                    checked = sum(row["status"] == "checked" for row in store.scenes())
                    receipt["scene_counts"] = {"checked": checked, "failed": len(manifest["scenes"]) - checked}
                finally:
                    store.close()
        receipt["finished_utc"] = now()
        if queue_owner is None:
            condition = {"IfMatch": result_etag} if result_etag else {"IfNoneMatch": "*"}
            client.put_object(
                Bucket=bucket, Key=result_key, Body=encode(receipt), ContentType="application/json", **condition
            )
        save_json(workspace / "receipt.json", receipt)
        if queue_owner is not None:
            task_queue.save_result(job_id, receipt)
        return receipt


def list_jobs(client, bucket: str, include_scenes: bool = False) -> list[dict]:
    rows = []
    for queued in task_queue.list_jobs():
        manifest = task_queue.client().get(task_queue.keys()["manifest"] + queued["job_id"])
        manifest = json.loads(manifest)
        if include_scenes:
            queued.update(created_utc=manifest["created_utc"], scene_details=[
                {"scene_id": scene["scene"], "source": scene["source"], "captured": scene["time"]}
                for scene in manifest["scenes"]
            ])
        rows.append(queued)
    return rows


def drain_jobs(client, bucket: str) -> list[dict]:
    results = []
    for job_id in task_queue.ready_ids():
        owner = f"{os.uname().nodename if hasattr(os, 'uname') else 'worker'}:{uuid.uuid4().hex}"
        manifest = task_queue.claim(job_id=job_id, owner=owner)
        if manifest is None:
            continue
        result = {"job_key": job_id, "job_id": job_id, "site": manifest["site"]["name"]}
        receipt = None
        try:
            receipt = run_job(client, bucket, manifest, queue_owner=owner)
            result.update(status=receipt["status"], run_no=receipt["run_no"], error=receipt.get("error"))
            if receipt["status"] == "succeeded":
                task_queue.acknowledge(job_id, owner)
            else:
                task_queue.fail(job_id, owner)
        except Exception as error:
            if receipt is None:
                task_queue.fail_with_result(job_id, owner, manifest, error)
            else:
                task_queue.fail(job_id, owner)
            result.update(status="failed", error=f"{type(error).__name__}: {error}")
        results.append(result)
        print(json.dumps(result), flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor worker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("drain")
    run_parser = commands.add_parser("run")
    source = run_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--job-id")
    args = parser.parse_args()
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    bucket = env("S3_BUCKET_NAME")
    if args.command == "list":
        print(json.dumps(list_jobs(client, bucket), indent=2))
        return 0
    if args.command == "drain":
        results = drain_jobs(client, bucket)
        print(json.dumps({"processed": len(results), "failed": sum(row["status"] != "succeeded" for row in results)}))
        return int(any(row["status"] != "succeeded" for row in results))
    queue_owner = None
    if args.job_id:
        queue_owner = f"{os.uname().nodename if hasattr(os, 'uname') else 'worker'}:{uuid.uuid4().hex}"
        manifest = task_queue.claim(args.job_id, queue_owner)
        if manifest is None:
            raise RuntimeError("Redis task is not pending or is already claimed")
    elif args.manifest:
        manifest = json.loads(args.manifest.read_bytes())
    if queue_owner is not None:
        owner = task_queue.client().get(task_queue.keys()["owner"] + manifest["job_id"])
        if owner != queue_owner:
            raise RuntimeError("Redis claim was not acquired")
    receipt = None
    try:
        receipt = run_job(client, bucket, manifest, args.resume, queue_owner)
        if queue_owner:
            if receipt["status"] == "succeeded":
                task_queue.acknowledge(manifest["job_id"], queue_owner)
            else:
                task_queue.fail(manifest["job_id"], queue_owner)
    except Exception as error:
        if queue_owner:
            if receipt is None:
                task_queue.fail_with_result(manifest["job_id"], queue_owner, manifest, error)
            else:
                task_queue.fail(manifest["job_id"], queue_owner)
        raise
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
