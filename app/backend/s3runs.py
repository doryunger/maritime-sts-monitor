import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from app.backend import ledger
from maritime_tip_cue.config import env, settings
from maritime_tip_cue.watch import registry

INTERVAL_SECONDS = 300
_missing: set[str] = set()


def folder_name(job_id: str, attempt_id: str) -> str:
    return f"{job_id[:12]}-{attempt_id[:8]}"


def succeeded_attempts(jobs: list[dict]):
    for job in jobs:
        if job["status"] != "succeeded":
            continue
        done = [a for a in job["attempts"] if a["verdict"] == "succeeded" and a.get("attempt_id")]
        if done:
            yield job["job_id"], done[-1]["attempt_id"]


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
    if not (target / "state.db").is_file():
        raise ValueError("review data has no run database")


def legacy_runs(client, bucket: str) -> list[tuple[str, str]]:
    base = settings()["catalogue"]["reviews_prefix"].rstrip("/") + "/legacy/"
    found = []
    token = None
    while True:
        args = {"Bucket": bucket, "Prefix": base, "Delimiter": "/"}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        found += [(item["Prefix"].rstrip("/").rsplit("/", 1)[1], item["Prefix"]) for item in page.get("CommonPrefixes", [])]
        if not page.get("IsTruncated"):
            return found
        token = page["NextContinuationToken"]


def sync_once(client, bucket: str, jobs: list[dict] | None = None) -> dict:
    jobs = ledger.fetch()["jobs"] if jobs is None else jobs
    root = registry.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    known = {row["folder"] for row in registry.listing()}
    added, errors = [], []
    wanted = [
        (folder_name(job_id, attempt_id), settings()["catalogue"]["reviews_prefix"].rstrip("/") + f"/{job_id}/{attempt_id}/")
        for job_id, attempt_id in succeeded_attempts(jobs)
    ]
    wanted += [(f"legacy-{name}", prefix) for name, prefix in legacy_runs(client, bucket)]
    for name, prefix in wanted:
        if name in known or name in _missing:
            continue
        temporary = root / f".tmp-{name}"
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            download(client, bucket, prefix, temporary)
            final = root / name
            temporary.rename(final)
            registry.register(final)
            added.append(name)
        except ClientError as error:
            shutil.rmtree(temporary, ignore_errors=True)
            if error.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
                _missing.add(name)
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
