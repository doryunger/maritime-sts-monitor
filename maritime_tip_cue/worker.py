import argparse
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from maritime_tip_cue import ledger, pg, task_queue
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


def export_parquets(store, destination: Path, job_id: str) -> list[Path]:
    import pyarrow as pa
    import pyarrow.parquet as pq
    from shapely.geometry import Point

    from maritime_tip_cue.watch.pgutil import iso

    def text(value):
        return iso(value) if isinstance(value, datetime) else str(value)

    destination.mkdir(parents=True, exist_ok=True)
    key = store.run_key
    run_no = store.run_no
    matches: dict = {}
    for item in store.db.execute("select * from review.ais_matches where run_key = %s order by id", (key,)):
        matches.setdefault((item["scene_id"], item["site"], item["hull_id"]), []).append(
            {k: v for k, v in item.items() if k not in ("run_key", "id")}
        )
    coverage = {
        (item["scene_id"], item["site"]): {k: v for k, v in item.items() if k != "run_key"}
        for item in store.db.execute("select * from review.ais_coverage where run_key = %s", (key,))
    }
    rows = []
    query = (
        "select h.*, s.source, s.capture_utc, f.run_verdict, f.verdict as final_verdict, f.qa_status, f.qa_note "
        "from review.hulls h join review.scenes s on s.run_key = h.run_key and s.scene_id = h.scene_id and s.site = h.site "
        "left join review.findings f on f.run_key = h.run_key and f.scene_id = h.scene_id and f.site = h.site and f.hull_id = h.hull_id "
        "where h.run_key = %s order by h.site, h.scene_id, h.hull_id"
    )
    for row in store.db.execute(query, (key,)):
        found = matches.get((row["scene_id"], row["site"], row["hull_id"]))
        cover = coverage.get((row["scene_id"], row["site"]))
        row.update(
            job_id=job_id,
            run_no=run_no,
            record=json.dumps(row["record"]),
            capture_utc=iso(row["capture_utc"]),
            ais_matches=json.dumps(found, default=text) if found else None,
            ais_coverage=json.dumps(cover, default=text) if cover else None,
        )
        row["geometry"] = Point(row["lon"], row["lat"]).wkb if row["lat"] is not None and row["lon"] is not None else None
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
        row | {"feature": json.dumps(row["feature"]), "job_id": job_id, "run_no": run_no}
        for row in store.db.execute(
            "select scene_id, site, idx, col, row, lat, lon, score, feature from review.raw_detections "
            "where run_key = %s order by site, scene_id, idx",
            (key,),
        )
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
    return [path, raw_path]


def upload_artifacts(client, bucket: str, manifest: dict, paths: list[Path], attempt: str) -> list[dict]:
    if manifest["site"].get("pipeline") == "hotspot_test":
        from maritime_tip_cue import hotspot_tests

        base = hotspot_tests.artifacts_prefix().rstrip("/")
    else:
        base = settings()["catalogue"]["artifacts_prefix"].rstrip("/")
    prefix = base + f"/{manifest['job_id']}/{attempt}/"
    artifacts = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = prefix + path.name
        client.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}})
        artifacts.append({"key": key, "sha256": digest, "size": path.stat().st_size, "kind": path.name})
    return artifacts


REVIEW_SKIP_DIRS = {"cache", "work"}


def upload_review(client, bucket: str, manifest: dict, out: Path, run_no: int, attempt: str) -> dict:
    if manifest["site"].get("pipeline") == "hotspot_test":
        from maritime_tip_cue import hotspot_tests

        base = hotspot_tests.reviews_prefix().rstrip("/")
    else:
        base = settings()["catalogue"]["reviews_prefix"].rstrip("/")
    prefix = base + f"/{manifest['job_id']}/{attempt}/"
    meta = {"job_id": manifest["job_id"], "attempt_id": attempt, "run_no": run_no}
    return upload_review_folder(client, bucket, prefix, out, meta)


def upload_review_folder(client, bucket: str, prefix: str, out: Path, meta: dict) -> dict:
    files = []
    sources = []
    for path in sorted(out.rglob("*")):
        relative = path.relative_to(out)
        if not path.is_file() or REVIEW_SKIP_DIRS & set(relative.parts[:-1]):
            continue
        sources.append((path, relative.as_posix()))
    for path, relative in sources:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        client.upload_file(str(path), bucket, prefix + relative, ExtraArgs={"Metadata": {"sha256": digest}})
        files.append({"path": relative, "size": path.stat().st_size, "sha256": digest})
    index = {"schema_version": 1, **meta, "files": files}
    client.put_object(Bucket=bucket, Key=prefix + "index.json", Body=encode(index), ContentType="application/json")
    return {"prefix": prefix, "index": prefix + "index.json", "files": len(files)}


def publish_progress(job_id: str) -> None:
    if not ACTIVE:
        return
    from maritime_tip_cue.watch.pgutil import iso

    with pg.connect() as conn:
        rows = conn.execute(
            "select s.scene_id, s.status, s.stage, s.updated_utc, s.error from review.scenes s "
            "join review.runs r on r.run_key = s.run_key where r.run_no = %s",
            (ACTIVE["run_no"],),
        ).fetchall()
    task_queue.save_progress(
        job_id,
        {r[0]: {"status": r[1], "stage": r[2], "updated_utc": iso(r[3]), "error": r[4]} for r in rows},
    )


def provenance_of(manifest: dict, attempt: str, review: dict | None) -> dict:
    from maritime_tip_cue.watch import run as runmod

    return {
        "attempt_id": attempt,
        "review_prefix": review["prefix"] if review else None,
        "rules_sha256": manifest["rules_sha256"],
        "code_commit": runmod.git_head() or None,
        "detector_revision": os.environ.get("STS_DETECTOR_REVISION") or None,
    }


def publish_pairs(client, bucket: str, out: Path, run_no: int, job_id: str, rules_path: Path, provenance: dict | None = None) -> dict:
    from maritime_tip_cue import publish
    from maritime_tip_cue.sts import rules as rules_mod

    try:
        return publish.publish_run(client, bucket, out, run_no, job_id, rules_mod.load(rules_path), provenance)
    except Exception as error:
        raise RuntimeError(f"publish failed: {type(error).__name__}: {error}") from error


def upload_hotspot_test_findings(client, bucket: str, manifest: dict, path: Path) -> list[dict]:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from maritime_tip_cue import hotspot_tests

    table = pq.read_table(path)
    roles = table["role"]
    verdicts = table["run_verdict"]
    keep = pc.and_(
        pc.is_in(verdicts, value_set=pa.array(["pair", "possible_pair"])),
        pc.invert(pc.is_in(roles, value_set=pa.array(["duplicate", "hull_b_box"]))),
    )
    table = table.filter(keep).drop(["ais_matches", "ais_coverage"])
    site = manifest["site"]
    table = table.append_column("area_version", pa.array([site["area_version"]] * table.num_rows, type=pa.int32()))
    table = table.append_column("pipeline", pa.array(["hotspot_test"] * table.num_rows, type=pa.string()))
    groups = {}
    for row in table.to_pylist():
        day = row["capture_utc"][:10]
        groups.setdefault((day, row["source"]), []).append(row)
    scene_groups = {}
    for scene in manifest["scenes"]:
        day = scene["time"][:10]
        scene_groups.setdefault((day, scene["source"]), [])
    outputs = []
    base = hotspot_tests.findings_prefix()
    for (day, source), rows in sorted(scene_groups.items()):
        selected = groups.get((day, source), [])
        output = path.parent / f"hotspot_pairs_{day}_{source}.parquet"
        filtered = pa.Table.from_pylist(selected, schema=table.schema)
        pq.write_table(filtered, output)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        key = f"{base}{day}/{site['name']}/{source}/{manifest['job_id']}.parquet"
        client.upload_file(str(output), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}})
        outputs.append({"key": key, "sha256": digest, "size": output.stat().st_size, "rows": len(selected), "date": day, "source": source})
    return outputs


def attach_review(receipt: dict, client, bucket: str, manifest: dict, out: Path, run_no, attempt: str):
    try:
        receipt["review"] = upload_review(client, bucket, manifest, out, run_no, attempt)
    except Exception as error:
        receipt["review"] = None
        receipt["review_error"] = f"{type(error).__name__}: {error}"


ACTIVE: dict = {}


def await_attempt(job_id: str, attempt_id: str, owner: str) -> None:
    cfg = settings()["worker"]
    limit = float(cfg.get("attempt_wait_seconds", 120))
    interval = float(cfg.get("attempt_poll_seconds", 2))
    with pg.connect() as conn:
        ledger.ensure_attempt(conn, job_id, attempt_id, owner)
        conn.commit()
        deadline = time.monotonic() + limit
        while not conn.execute("select 1 from processing.job_attempts where attempt_id = %s", (attempt_id,)).fetchone():
            conn.rollback()
            if time.monotonic() >= deadline:
                raise RuntimeError(f"attempt {attempt_id} of job {job_id} is not in the job ledger after {limit:.0f} s")
            time.sleep(interval)


def open_store(manifest: dict, queue_owner: str | None, workspace: Path):
    from maritime_tip_cue.watch.state import Store

    attempt = manifest.get("_queue_attempt_id")
    pointer = workspace / "run.json"
    if queue_owner is not None:
        await_attempt(manifest["job_id"], attempt, queue_owner)
        store = Store.for_attempt(attempt) or Store.create("job", attempt, manifest["job_id"])
    elif pointer.exists():
        return Store.open(json.loads(pointer.read_bytes())["run_no"])
    else:
        store = Store.create("legacy")
    save_json(pointer, {"run_no": store.run_no})
    return store


def _run_job(client, bucket: str, manifest: dict, resume: bool = False, queue_owner: str | None = None) -> dict:
    validate(manifest)
    root = worker_root()
    with local_lock(root):
        job_id = manifest["job_id"]
        result_key = key_for("results", job_id)
        previous, result_etag = json_object(client, bucket, result_key)
        if previous and previous["status"] == "succeeded" and queue_owner is None:
            if previous.get("job_id") != job_id or previous.get("manifest_sha256") != job_id:
                raise RuntimeError("existing receipt does not match manifest")
            return previous
        ACTIVE.clear()
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
            "attempt_id": manifest.get("_queue_attempt_id"),
            "manifest_sha256": job_id,
            "status": "failed",
            "run_no": None,
            "started_utc": now(),
            "finished_utc": None,
            "artifacts": [],
            "scene_counts": {"checked": 0, "failed": len(manifest["scenes"])},
            "error": None,
        }
        attempt = manifest.get("_queue_attempt_id") or uuid.uuid4().hex
        store = open_store(manifest, queue_owner, workspace)
        run_no, out = store.run_no, store.path
        receipt["run_no"] = run_no
        ACTIVE.update(out=out, run_no=run_no, workspace=workspace, attempt=attempt, rules_path=rules_path)
        try:
            from maritime_tip_cue.watch import jobs

            receipt["scene_counts"] = jobs.execute(manifest, store, rules_path)
            paths = export_parquets(store, workspace / attempt, job_id)
            paths += [workspace / "manifest.json", rules_path]
            receipt["artifacts"] = upload_artifacts(client, bucket, manifest, paths, attempt)
            attach_review(receipt, client, bucket, manifest, out, run_no, attempt)
            if manifest["site"].get("pipeline") == "hotspot_test":
                receipt["hotspot_test_outputs"] = upload_hotspot_test_findings(client, bucket, manifest, paths[0])
            else:
                receipt["published"] = publish_pairs(
                    client, bucket, out, run_no, job_id, rules_path, provenance_of(manifest, attempt, receipt["review"])
                )
            receipt["status"] = "succeeded"
        except Exception as error:
            receipt["error"] = f"{type(error).__name__}: {error}"
            try:
                checked = checked_scenes(run_no)
                receipt["scene_counts"] = {"checked": checked, "failed": len(manifest["scenes"]) - checked}
                paths = [workspace / "manifest.json", rules_path]
                receipt["artifacts"] = upload_artifacts(client, bucket, manifest, paths, attempt)
                attach_review(receipt, client, bucket, manifest, out, run_no, attempt)
            except Exception as record_error:
                receipt["error"] += f"; run record not uploaded: {type(record_error).__name__}: {record_error}"
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
            task_queue.save_result(job_id, queue_owner, receipt)
        return receipt


def checked_scenes(run_no: int) -> int:
    from maritime_tip_cue.watch.state import Store

    store = Store.open(run_no)
    try:
        return sum(row["status"] == "checked" for row in store.scenes())
    finally:
        store.close()


def record_stop(client, bucket: str, manifest: dict, queue_owner: str, attempt_id: str, reason: str) -> None:
    receipt = {
        "schema_version": 1,
        "job_id": manifest["job_id"],
        "attempt_id": attempt_id,
        "manifest_sha256": manifest["job_id"],
        "status": "stopped",
        "run_no": ACTIVE.get("run_no"),
        "started_utc": None,
        "finished_utc": now(),
        "artifacts": [],
        "scene_counts": {"checked": 0, "failed": len(manifest["scenes"])},
        "error": reason,
    }
    try:
        if ACTIVE:
            checked = checked_scenes(ACTIVE["run_no"])
            receipt["scene_counts"] = {"checked": checked, "failed": len(manifest["scenes"]) - checked}
            paths = [ACTIVE["workspace"] / "manifest.json", ACTIVE["rules_path"]]
            receipt["artifacts"] = upload_artifacts(client, bucket, manifest, paths, attempt_id)
            attach_review(receipt, client, bucket, manifest, ACTIVE["out"], ACTIVE["run_no"], attempt_id)
    except Exception as error:
        receipt["error"] += f"; run record not uploaded: {type(error).__name__}: {error}"
    try:
        task_queue.save_result(manifest["job_id"], queue_owner, receipt)
    except Exception:
        pass


def run_job(client, bucket: str, manifest: dict, resume: bool = False, queue_owner: str | None = None) -> dict:
    if queue_owner is None:
        return _run_job(client, bucket, manifest, resume, queue_owner)
    attempt_id = manifest.get("_queue_attempt_id")
    if not attempt_id:
        raise ValueError("Queued worker manifest has no attempt identity")
    from maritime_tip_cue.config import settings

    cfg = settings()["worker"]["queue"]
    interval = max(1, int(cfg["heartbeat_seconds"]))
    lease = int(cfg["lease_seconds"])
    per_scene = cfg.get("max_seconds_per_scene")
    deadline = time.monotonic() + int(per_scene) * len(manifest["scenes"]) if per_scene else None
    stopped = threading.Event()

    def abort(reason):
        print(json.dumps({"job_id": manifest["job_id"], "aborted": reason}), file=sys.stderr, flush=True)
        recorder = threading.Thread(
            target=record_stop, args=(client, bucket, manifest, queue_owner, attempt_id, reason), daemon=True
        )
        recorder.start()
        recorder.join(timeout=120)
        os._exit(75)

    def maintain_lease():
        last_renewed = time.monotonic()
        while not stopped.wait(interval):
            try:
                publish_progress(manifest["job_id"])
            except Exception:
                pass
            if deadline is not None and time.monotonic() > deadline:
                abort("maximum runtime exceeded")
            try:
                if not task_queue.heartbeat(manifest["job_id"], queue_owner, attempt_id):
                    abort("lease lost")
                last_renewed = time.monotonic()
            except Exception:
                if time.monotonic() - last_renewed > lease:
                    abort("heartbeat unreachable past lease")

    heartbeat_thread = threading.Thread(target=maintain_lease, daemon=True)
    heartbeat_thread.start()
    try:
        receipt = _run_job(client, bucket, manifest, resume, queue_owner)
        return receipt
    finally:
        stopped.set()
        heartbeat_thread.join(timeout=interval + 1)
        try:
            publish_progress(manifest["job_id"])
        except Exception:
            pass


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
    if not settings()["worker"]["queue"].get("max_seconds_per_scene"):
        raise RuntimeError("drain requires worker.queue.max_seconds_per_scene in the worker config")
    results = []
    attempted = set()
    while True:
        pending = [job_id for job_id in task_queue.ready_ids() if job_id not in attempted]
        if not pending:
            return results
        for job_id in pending:
            attempted.add(job_id)
            owner = f"{os.uname().nodename if hasattr(os, 'uname') else 'worker'}:{uuid.uuid4().hex}"
            manifest = task_queue.claim(job_id=job_id, owner=owner)
            if manifest is None:
                continue
            result = {"job_key": job_id, "job_id": job_id, "site": manifest["site"]["name"]}
            receipt = None
            try:
                receipt = run_job(client, bucket, manifest, queue_owner=owner)
                result.update(status=receipt["status"], run_no=receipt["run_no"], error=receipt.get("error"))
            except Exception as error:
                if receipt is None:
                    task_queue.fail_with_result(job_id, owner, manifest, error)
                result.update(status="failed", error=f"{type(error).__name__}: {error}")
            results.append(result)
            print(json.dumps(result), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor worker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("drain")
    run_parser = commands.add_parser("run")
    source = run_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--job-id")
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--unledgered", action="store_true")
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
    if args.manifest and not args.unledgered:
        parser.error("a manifest run is not recorded in the job ledger; use --job-id, or pass --unledgered for a test")
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
    except Exception as error:
        if queue_owner:
            if receipt is None:
                task_queue.fail_with_result(manifest["job_id"], queue_owner, manifest, error)
        raise
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
