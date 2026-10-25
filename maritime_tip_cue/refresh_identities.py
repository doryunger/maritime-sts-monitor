import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

from maritime_tip_cue import publish_merge as merge
from maritime_tip_cue.ais import match
from maritime_tip_cue.config import env, settings

FIELDS = ("imo", "mmsi", "flag")


def missing(row: dict, n: int) -> bool:
    return row.get(f"vessel_{n}_ship_id") is not None and any(row.get(f"vessel_{n}_{field}") is None for field in FIELDS)


def fill(rows: list[dict], identities: dict) -> int:
    changed = 0
    for row in rows:
        for n in (1, 2):
            ident = identities.get(row.get(f"vessel_{n}_ship_id"))
            if not ident:
                continue
            for field in FIELDS:
                key = f"vessel_{n}_{field}"
                if row.get(key) is None and ident.get(field) not in (None, ""):
                    row[key] = str(ident[field])
                    changed += 1
    return changed


def refresh(client, bucket: str, loader=match.load_identities, upload: bool = True) -> dict:
    prefix = settings()["s3"]["findings_prefix"]
    summary = {"files": 0, "rows": 0, "slots_filled": 0, "slots_still_missing": 0}
    for key in merge.list_keys(client, bucket, prefix):
        if not key.endswith(".parquet"):
            continue
        for _ in range(merge.ATTEMPTS):
            rows, etag = merge.read_rows(client, bucket, key)
            wanted = sorted({r[f"vessel_{n}_ship_id"] for r in rows for n in (1, 2) if missing(r, n)})
            if not wanted:
                break
            changed = fill(rows, loader(wanted))
            if not changed:
                summary["slots_still_missing"] += sum(missing(r, n) for r in rows for n in (1, 2))
                break
            try:
                if upload:
                    merge.write_rows(client, bucket, key, rows, etag)
            except ClientError as error:
                if error.response["Error"]["Code"] not in merge.CONFLICT:
                    raise
                continue
            summary["files"] += 1
            summary["slots_filled"] += changed
            summary["slots_still_missing"] += sum(missing(r, n) for r in rows for n in (1, 2))
            break
        summary["rows"] += len(rows)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor refresh-identities")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    print(json.dumps(refresh(client, env("S3_BUCKET_NAME"), upload=not args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
