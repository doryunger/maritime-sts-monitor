import sqlite3
from datetime import UTC, datetime

import boto3
from botocore.config import Config

from app.backend.runs import scene_status, status_of
from maritime_tip_cue.config import env
from maritime_tip_cue.watch import registry
from maritime_tip_cue.worker import list_jobs


def local_scenes() -> list[dict]:
    rows = []
    for run in registry.listing():
        path = registry.runs_root() / run["folder"] / "state.db"
        if not path.exists():
            continue
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            invocations = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY run_id")]
            event = db.execute("SELECT MAX(time_utc) FROM events").fetchone()[0]
            status = status_of(invocations, event)
            for scene in db.execute("SELECT scene_id, site, source, capture_utc, status FROM scenes"):
                rows.append({
                    "run": run["run_no"], "site": scene["site"], "scene_id": scene["scene_id"],
                    "source": scene["source"], "captured": scene["capture_utc"],
                    "status": scene_status(scene["status"], status),
                })
        finally:
            db.close()
    return rows


def snapshot() -> dict:
    local = local_scenes()
    index = {}
    for scene in local:
        index.setdefault((scene["site"], scene["source"], scene["scene_id"]), []).append(scene)
    result = {"checked_utc": datetime.now(UTC).isoformat(), "jobs": None, "local": local, "error": None}
    try:
        client = boto3.client("s3", region_name=env("AWS_REGION"), config=Config(
            connect_timeout=5, read_timeout=10, retries={"max_attempts": 1},
        ))
        jobs = list_jobs(client, env("S3_BUCKET_NAME"), include_scenes=True)
        for job in jobs:
            for scene in job["scene_details"]:
                scene["local_runs"] = index.get((job["site"], scene["source"], scene["scene_id"]), [])
        result["jobs"] = sorted(jobs, key=lambda row: (row["created_utc"], row["job_id"]), reverse=True)
    except Exception:
        result["error"] = "Could not read the shared queue. Check the server's AWS access and retry."
    return result
