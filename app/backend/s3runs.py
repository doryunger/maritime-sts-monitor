import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from maritime_tip_cue.config import env, settings
from maritime_tip_cue.watch import registry
from maritime_tip_cue.watch.registry import fetch

INTERVAL_SECONDS = 300
RETRY_MISSING_SECONDS = 1800
_missing: dict[str, float] = {}


def folder_name(job_id: str, attempt_id: str) -> str:
    return f"{job_id[:12]}-{attempt_id[:8]}"


def download(client, bucket: str, prefix: str, target: Path) -> None:
    index = json.loads(client.get_object(Bucket=bucket, Key=prefix + "index.json")["Body"].read())
    for entry in index["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe path in review index: {entry['path']}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, prefix + entry["path"], str(destination))
        data = destination.read_bytes()
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"checksum mismatch for {entry['path']}")


def review_prefix(row: dict) -> str | None:
    base = settings()["catalogue"]["reviews_prefix"].rstrip("/")
    if row["origin"] == "legacy":
        return f"{base}/legacy/run-{row['run_no']}/"
    if row["job_id"] and row["attempt_id"]:
        return f"{base}/{row['job_id']}/{row['attempt_id']}/"
    return None


def wanted_runs() -> list[dict]:
    return fetch(None, "select run_key, run_no, origin, folder, attempt_id, job_id, attempt_status from review.run_lineage order by run_no")


def sync_once(client, bucket: str) -> dict:
    root = registry.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    added, errors = [], []
    for row in wanted_runs():
        name = row["folder"]
        final = root / name
        prefix = review_prefix(row)
        if final.is_dir() or prefix is None or row.get("attempt_status") == "running":
            continue
        if time.time() - _missing.get(name, 0.0) < RETRY_MISSING_SECONDS:
            continue
        temporary = root / f".tmp-{name}"
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            download(client, bucket, prefix, temporary)
            temporary.rename(final)
            added.append(name)
        except ClientError as error:
            shutil.rmtree(temporary, ignore_errors=True)
            first = name not in _missing
            if error.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
                _missing[name] = time.time()
                if not first:
                    continue
            errors.append({"run": name, "error": str(error)})
        except Exception as error:
            shutil.rmtree(temporary, ignore_errors=True)
            errors.append({"run": name, "error": f"{type(error).__name__}: {error}"})
    return {"added": added, "errors": errors}


def start(interval: int = INTERVAL_SECONDS) -> threading.Thread:
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    bucket = env("S3_BUCKET_NAME")

    def loop():
        while True:
            try:
                result = sync_once(client, bucket)
                if result["added"] or result["errors"]:
                    print(json.dumps(result), flush=True)
            except Exception as error:
                print(f"run sync failed: {type(error).__name__}: {error}", flush=True)
            time.sleep(interval)

    thread = threading.Thread(target=loop, daemon=True, name="s3-run-sync")
    thread.start()
    return thread
