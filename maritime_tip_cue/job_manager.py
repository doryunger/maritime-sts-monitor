import argparse
import json
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import boto3

from maritime_tip_cue import catalogue, task_queue
from maritime_tip_cue.config import ROOT, env, settings


def database_path():
    path = Path(settings()["catalogue"]["database"])
    return path if path.is_absolute() else ROOT / path


def clients():
    cfg = settings()["catalogue"]
    return task_queue.client(), boto3.client("s3", region_name=env("AWS_REGION")), env("S3_BUCKET_NAME"), cfg


def ensure_group(redis):
    try:
        redis.xgroup_create(task_queue.keys()["events"], "job-manager", id="0", mkstream=True)
    except Exception as error:
        if "BUSYGROUP" not in str(error):
            raise


def read_events(redis):
    stream = task_queue.keys()["events"]
    rows = []
    for marker in ("0", ">"):
        batch = redis.xreadgroup("job-manager", "tracker", {stream: marker}, count=100)
        rows.extend((event_id, fields) for _, entries in batch for event_id, fields in entries)
    return rows


def cycle(redis, s3, bucket, cfg, events):
    path = database_path()
    with catalogue.controller_lock(path), closing(catalogue.connect(path)) as db:
        queue_cfg = settings()["worker"]["queue"]
        expired = task_queue.requeue_stale()
        completed, errors = catalogue.reconcile_redis(
            db,
            redis,
            s3,
            bucket,
            cfg["artifacts_prefix"],
            events,
            int(queue_cfg["max_retries"]),
            int(queue_cfg["retry_delay_seconds"]),
        )
        done = {row[0] for row in db.execute("SELECT job_id FROM jobs WHERE status='succeeded'")}
        failed = {row[0] for row in db.execute("SELECT job_id FROM jobs WHERE status='failed'")}
        retry_wait = {
            row["job_id"]: int(datetime.fromisoformat(row["available_utc"]).timestamp() * 1000)
            for row in db.execute(
                "SELECT job_id,available_utc FROM jobs "
                "WHERE status='retry_wait' AND available_utc IS NOT NULL"
            )
        }
        task_queue.sync_terminal(done, failed)
        task_queue.sync_retry_wait(retry_wait)
        queued = catalogue.refresh_queue(db)
    return {"finalized": completed, "expired_leases": expired, "queued": queued, "errors": errors}


def run(interval):
    redis, s3, bucket, cfg = clients()
    while True:
        try:
            ensure_group(redis)
            events = read_events(redis)
            result = cycle(redis, s3, bucket, cfg, events)
            if events:
                ids = [entry_id for entry_id, _ in events]
                redis.xack(task_queue.keys()["events"], "job-manager", *ids)
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as error:
            print(json.dumps({"error": f"{type(error).__name__}: {error}"}), flush=True)
        time.sleep(interval)


def scan_and_dispatch_one():
    redis = task_queue.client()
    cfg = settings()["catalogue"]
    path = database_path()
    with catalogue.controller_lock(path), closing(catalogue.connect(path)) as db:
        scan = catalogue.scan(db, cfg, first_scene=True)
        job_id = scan.get("selected_job_id")
        dispatched = catalogue.refresh_one_job(db, job_id) if job_id else None
    print(json.dumps({"scan": scan, "dispatched": dispatched}, sort_keys=True))
    return 1 if scan.get("status") == "failed" else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "once", "scan-one"))
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    if args.job_id and args.command != "once":
        parser.error("--job-id is only valid with once")
    if args.command == "run":
        run(args.interval)
        return 0
    if args.command == "scan-one":
        return scan_and_dispatch_one()
    if args.job_id:
        redis = task_queue.client()
        path = database_path()
        with catalogue.controller_lock(path), closing(catalogue.connect(path)) as db:
            dispatched = catalogue.refresh_one_job(db, args.job_id)
        print(json.dumps({"dispatched": dispatched}, sort_keys=True))
        return 0
    redis, s3, bucket, cfg = clients()
    ensure_group(redis)
    events = read_events(redis)
    print(json.dumps(cycle(redis, s3, bucket, cfg, events), sort_keys=True))
    if events:
        redis.xack(task_queue.keys()["events"], "job-manager", *(event_id for event_id, _ in events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
