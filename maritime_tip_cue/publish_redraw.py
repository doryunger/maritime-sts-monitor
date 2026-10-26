import argparse
import io
import json
import sys
import tempfile
import time
from pathlib import Path

import pyarrow.parquet as pq

from maritime_tip_cue import publish
from maritime_tip_cue.config import DATA_DIR, env, settings
from maritime_tip_cue.publish_merge import list_keys
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch.registry import fetch


def published_rows(client, bucket: str) -> list[dict]:
    rows = []
    for key in list_keys(client, bucket, settings()["s3"]["findings_prefix"]):
        if key.endswith(".parquet"):
            table = pq.read_table(io.BytesIO(client.get_object(Bucket=bucket, Key=key)["Body"].read()))
            rows += [dict(row, parquet_key=key) for row in table.to_pylist()]
    return rows


def runs_of(row: dict) -> list[int]:
    found = fetch(
        None,
        "select r.run_no from review.findings f join review.runs r on r.run_key = f.run_key "
        "where f.scene_id = %s and f.site = %s and f.hull_id = %s and f.verdict = 'pair' order by r.run_no",
        (row["scene_id"], row["hot_spot"], row["hull_id"]),
    )
    runs = [r["run_no"] for r in found]
    return [row["run"]] if row.get("run") in runs else runs


def redraw(client, bucket: str, rows: list[dict], rules: dict, folder_of, upload: bool, backup: Path | None, log=print, runs_for=runs_of) -> dict:
    todo = {row["finding_id"]: (row, runs_for(row)) for row in rows if row.get("capture_png") and row.get("qa_status") != "rejected"}
    summary = {"rows": len(todo), "redrawn": 0, "not_found": [], "uploaded": 0, "backed_up": 0}
    scratch = Path(tempfile.mkdtemp(prefix="redraw-"))
    for no in sorted({n for _, candidates in todo.values() for n in candidates}):
        targets = {(row["scene_id"], row["hot_spot"], row["hull_id"]): row for row, candidates in todo.values() if no in candidates}
        if not targets:
            continue
        found = publish.complete(publish.collect_at(folder_of(no), no, None, None, only=set(targets), enrich=False))
        seen = set()
        for f in found:
            row = targets[(f["scene_id"], f["site"], f["hull_id"])]
            png = publish.write_captures(f, row["finding_id"], rules, scratch).get("png")
            if png is None:
                continue
            seen.add(row["finding_id"])
            todo.pop(row["finding_id"], None)
            summary["redrawn"] += 1
            if upload:
                key = row["capture_png"]
                if backup is not None:
                    old = backup / key
                    old.parent.mkdir(parents=True, exist_ok=True)
                    old.write_bytes(client.get_object(Bucket=bucket, Key=key)["Body"].read())
                    summary["backed_up"] += 1
                client.put_object(Bucket=bucket, Key=key, Body=png.read_bytes(), ContentType="image/png")
                summary["uploaded"] += 1
        log(f"run {no}: {len(seen)} of {len(targets)} images")
    summary["not_found"] = sorted(todo)
    return summary


def main() -> int:
    import boto3

    parser = argparse.ArgumentParser(prog="maritime-sts-monitor publish_redraw")
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    bucket = env("S3_BUCKET_NAME")
    backup = DATA_DIR / "publish" / f"pair-captures-backup-{time.strftime('%Y%m%dT%H%M%S')}" if args.upload else None
    summary = redraw(client, bucket, published_rows(client, bucket), rules_mod.load(), publish.registry.folder_of, args.upload, backup)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
