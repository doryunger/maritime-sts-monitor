import argparse
import json
import sys

import boto3

from maritime_tip_cue import publish, worker
from maritime_tip_cue.config import env, settings
from maritime_tip_cue.sts import rules as rules_mod


def plan(run_no: int) -> dict:
    store = publish.Store.open(run_no)
    out = store.path
    try:
        job_id = publish.run_job_id(store)
        commit = store.db.execute(
            "select commit_hash from review.run_invocations where run_key = %s order by invocation_id desc limit 1", (store.run_key,)
        ).fetchone()["commit_hash"]
    finally:
        store.close()
    if job_id is None:
        raise SystemExit(f"run {run_no} was not created by a queue job")
    workspace = worker.worker_root() / job_id
    receipt = json.loads((workspace / "receipt.json").read_bytes())
    if receipt["status"] != "succeeded" or not receipt.get("attempt_id"):
        raise SystemExit(f"run {run_no}: its local receipt is not a succeeded queue attempt")
    manifest = json.loads((workspace / "manifest.json").read_bytes())
    return {"run": run_no, "out": out, "job_id": job_id, "attempt": receipt["attempt_id"], "commit": commit, "manifest": manifest, "workspace": workspace}


def legacy(runs: list[int], upload: bool) -> int:
    client = boto3.client("s3", region_name=env("AWS_REGION")) if upload else None
    bucket = env("S3_BUCKET_NAME")
    for no in runs:
        store = publish.Store.open(no)
        out = store.path
        try:
            job_id = publish.run_job_id(store)
            status = store.db.execute(
                "select status from review.run_invocations where run_key = %s order by invocation_id desc limit 1", (store.run_key,)
            ).fetchone()["status"]
        finally:
            store.close()
        if status != "done":
            raise SystemExit(f"run {no} has status {status}; only finished runs are uploaded")
        print(f"run {no}: legacy review data, job {job_id[:8] if job_id else 'none'}", flush=True)
        if not upload:
            continue
        prefix = settings()["catalogue"]["reviews_prefix"].rstrip("/") + f"/legacy/run-{no}/"
        meta = {"legacy": True, "run_no": no, "job_id": job_id}
        print(json.dumps({"run": no, "review": worker.upload_review_folder(client, bucket, prefix, out, meta)}), flush=True)
    if not upload:
        print("dry run; pass --upload to send")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor backfill")
    parser.add_argument("runs", nargs="+", type=int)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.legacy:
        return legacy(args.runs, args.upload)
    publish.check_runs(args.runs)
    plans = [plan(no) for no in args.runs]
    for item in plans:
        print(f"run {item['run']}: job {item['job_id'][:8]} attempt {item['attempt'][:8]}", flush=True)
    if not args.upload:
        print("dry run; pass --upload to send review data and pairs to S3")
        return 0
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    bucket = env("S3_BUCKET_NAME")
    rules = rules_mod.load()
    for item in plans:
        review = worker.upload_review(client, bucket, item["manifest"], item["out"], item["run"], item["attempt"])
        provenance = worker.provenance_of(item["manifest"], item["attempt"], review) | {"code_commit": item["commit"] or None}
        published = publish.publish_run(client, bucket, item["out"], item["run"], item["job_id"], rules, provenance)
        print(json.dumps({"run": item["run"], "review": review, "published": published}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
