import argparse
import json
import time
from contextlib import closing
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


def cycle(redis, s3, bucket, cfg):
    path = database_path()
    with catalogue.controller_lock(path), closing(catalogue.connect(path)) as db:
        completed, errors = catalogue.reconcile_redis(db, redis, s3, bucket, cfg["artifacts_prefix"])
        queued = catalogue.refresh_queue(db)
    return {"completed": completed, "queued": queued, "errors": errors}


def run(interval):
    redis, s3, bucket, cfg = clients()
    while True:
        try:
            ensure_group(redis)
            events = redis.xreadgroup("job-manager", "tracker", {task_queue.keys()["events"]: "0"}, count=100)
            if not events or not events[0][1]:
                events = redis.xreadgroup("job-manager", "tracker", {task_queue.keys()["events"]: ">"}, count=100, block=1)
            result = cycle(redis, s3, bucket, cfg)
            if events:
                ids = [entry_id for _, rows in events for entry_id, _ in rows]
                redis.xack(task_queue.keys()["events"], "job-manager", *ids)
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as error:
            print(json.dumps({"error": f"{type(error).__name__}: {error}"}), flush=True)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "once"))
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    if args.command == "run":
        run(args.interval)
        return 0
    redis, s3, bucket, cfg = clients()
    print(json.dumps(cycle(redis, s3, bucket, cfg), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
