import json

import boto3

from maritime_tip_cue.config import env, settings


def _client():
    return boto3.client("s3", region_name=env("AWS_REGION"))


def load() -> list[dict]:
    raw = json.loads(_client().get_object(Bucket=env("S3_BUCKET_NAME"), Key=settings()["s3"]["hotspots_key"])["Body"].read())
    default = raw["min_shore_distance_m"]["value"]
    return [
        {
            "name": s["name"],
            "bbox": s["bbox"],
            "min_shore_distance_m": s.get("min_shore_distance_m", {}).get("value", default),
        }
        for s in raw["sites"]["value"]
    ]


def publish(path: str) -> None:
    _client().upload_file(path, env("S3_BUCKET_NAME"), settings()["s3"]["hotspots_key"], ExtraArgs={"ContentType": "application/json"})


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "publish":
        publish(sys.argv[2])
    for h in load():
        print(f"{h['name']:<16} bbox {h['bbox']}  shore {h['min_shore_distance_m']} m")
