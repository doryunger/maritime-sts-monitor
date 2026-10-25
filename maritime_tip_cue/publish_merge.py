import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

from maritime_tip_cue.watch.geo import haversine_m

GEO = {"version": "1.0.0", "primary_column": "geometry", "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Point"]}}}
CONFLICT = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")
ATTEMPTS = 6


def length_of(row: dict) -> float:
    measures = json.loads(row.get("measures") or "{}")
    return measures.get("length_m") or measures.get("hull_a_length_m") or 0.0


def twin(a: dict, b: dict, rule: dict) -> bool:
    if a["source"] != b["source"]:
        return False
    if abs((a["capture_utc"] - b["capture_utc"]).total_seconds()) > rule["dedupe_time_s"]:
        return False
    reach = rule["dedupe_length_share"] * max(length_of(a), length_of(b))
    return haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= reach


def read_rows(client, bucket: str, key: str) -> tuple[list[dict], str | None]:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return [], None
        raise
    table = pq.read_table(io.BytesIO(response["Body"].read()))
    return table.to_pylist(), response.get("ETag")


OBSOLETE = {"capture_tif", "capture_markers_png"}


def encode_rows(rows: list[dict]) -> bytes:
    names = [n for n in dict.fromkeys(name for row in rows for name in row) if n not in OBSOLETE]
    table = pa.Table.from_pylist([{name: row.get(name) for name in names} for row in rows])
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), b"geo": json.dumps(GEO).encode()})
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def write_rows(client, bucket: str, key: str, rows: list[dict], etag: str | None) -> None:
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    client.put_object(Bucket=bucket, Key=key, Body=encode_rows(rows), ContentType="application/octet-stream", **condition)


def list_keys(client, bucket: str, prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        args = {"Bucket": bucket, "Prefix": prefix}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        keys += [item["Key"] for item in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        token = page["NextContinuationToken"]


def merge_group(client, bucket: str, prefix: str, date: str, key: str, new_rows: list[dict], job_id: str | None, rule: dict, on_keep=None) -> dict:
    for _ in range(ATTEMPTS):
        here, etag = read_rows(client, bucket, key)
        if job_id:
            here = [row for row in here if row.get("job_id") != job_id]
        by_id = {row["finding_id"]: row for row in here}
        others = {}
        for other_key in list_keys(client, bucket, f"{prefix}{date}/"):
            if other_key != key and other_key.endswith(".parquet"):
                others[other_key] = read_rows(client, bucket, other_key)
        kept, skipped, notes = [], [], {}
        for row in new_rows:
            if row["finding_id"] in by_id:
                by_id[row["finding_id"]] = row
                kept.append(row)
                continue
            owner = next(
                ((k, e) for k, (rows, _) in others.items() for e in rows if twin(row, e, rule)),
                None,
            ) or next(((key, e) for e in by_id.values() if twin(row, e, rule)), None)
            if owner is None:
                by_id[row["finding_id"]] = row
                kept.append(row)
            else:
                skipped.append(row["finding_id"])
                notes.setdefault(owner[0], []).append((owner[1]["finding_id"], f"{row['hot_spot']}/{row['finding_id']}"))
        merged = list(by_id.values())
        if on_keep:
            on_keep(kept)
        try:
            for other_key, entries in notes.items():
                if other_key == key:
                    for finding_id, text in entries:
                        row = by_id[finding_id]
                        row["also_in"] = ";".join(filter(None, [row.get("also_in"), text]))
            write_rows(client, bucket, key, merged, etag) if merged else None
            for other_key, entries in notes.items():
                if other_key != key:
                    rows, other_etag = others[other_key]
                    for finding_id, text in entries:
                        row = next(r for r in rows if r["finding_id"] == finding_id)
                        row["also_in"] = ";".join(filter(None, [row.get("also_in"), text]))
                    write_rows(client, bucket, other_key, rows, other_etag)
            return {"key": key, "kept": [r["finding_id"] for r in kept], "skipped": skipped, "rows": len(merged)}
        except ClientError as error:
            if error.response["Error"]["Code"] not in CONFLICT:
                raise
    raise RuntimeError(f"could not merge {key}: concurrent writers kept changing it")
