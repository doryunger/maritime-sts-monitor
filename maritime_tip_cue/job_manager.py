import argparse
import json
import time
from contextlib import closing

import boto3

from maritime_tip_cue import catalogue, task_queue
from maritime_tip_cue.config import env, settings


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
    with closing(catalogue.connect()) as db, catalogue.controller_lock(db):
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
        newly_superseded = catalogue.supersede_jobs(db)
        by_status = {}
        for row in catalogue.rows(db, "SELECT job_id,status,available_utc FROM processing.jobs"):
            by_status.setdefault(row["status"], []).append(row)
        task_queue.drop([row["job_id"] for row in by_status.get("superseded", [])])
        done = {row["job_id"] for row in by_status.get("succeeded", [])}
        failed = {row["job_id"] for row in by_status.get("failed", [])}
        retry_wait = {
            row["job_id"]: int(row["available_utc"].timestamp() * 1000)
            for row in by_status.get("retry_wait", [])
            if row["available_utc"] is not None
        }
        task_queue.sync_terminal(done, failed)
        task_queue.sync_retry_wait(retry_wait)
        queued = catalogue.refresh_queue(db)
    return {
        "finalized": completed,
        "expired_leases": expired,
        "queued": queued,
        "superseded": len(newly_superseded),
        "errors": errors,
    }


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
    cfg = settings()["catalogue"]
    with closing(catalogue.connect()) as db, catalogue.controller_lock(db):
        scan = catalogue.scan(db, cfg, first_scene=True)
        job_id = scan.get("selected_job_id")
        dispatched = catalogue.refresh_one_job(db, job_id) if job_id else None
    print(json.dumps({"scan": scan, "dispatched": dispatched}, sort_keys=True))
    return 1 if scan.get("status") == "failed" else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "once", "scan-one", "queue-status"))
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
    if args.command == "queue-status":
        print(json.dumps(task_queue.counts(), sort_keys=True))
        return 0
    if args.command == "scan-one":
        return scan_and_dispatch_one()
    if args.job_id:
        with closing(catalogue.connect()) as db, catalogue.controller_lock(db):
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
