from datetime import UTC, datetime

from app.backend import ledger

STATUS = {"pending": "ready", "dispatched": "ready", "running": "claimed"}
VERDICT = {"lease_expired": "stopped"}


def attempt(row: dict) -> dict:
    owner = (row.get("owner") or "").split(":")[0]
    return {
        "attempt_no": row["attempt_no"],
        "verdict": VERDICT.get(row["verdict"], row["verdict"]),
        "worker": owner or None,
        "finished_utc": row.get("finished_utc"),
        "error": row.get("error"),
        "before_requeue": bool(row.get("before_requeue")),
    }


def snapshot() -> dict:
    result = {"checked_utc": datetime.now(UTC).isoformat(), "jobs": None, "local": [], "error": None}
    try:
        jobs = []
        for job in ledger.fetch()["jobs"]:
            jobs.append(
                {
                    "job_key": job["job_id"],
                    "job_id": job["job_id"],
                    "site": job["site"],
                    "scenes": len(job["scenes"]),
                    "status": STATUS.get(job["status"], job["status"]),
                    "created_utc": job["created_utc"],
                    "last_update_utc": job.get("last_update_utc") or job["created_utc"],
                    "source": job["source"],
                    "error": job.get("error"),
                    "superseded_by": job["superseded_by"],
                    "attempts": [attempt(row) for row in job["attempts"]],
                    "scene_details": [dict(scene, local_runs=[]) for scene in job["scenes"]],
                }
            )
        result["jobs"] = jobs
    except Exception:
        result["error"] = "Could not read the ledger on the control host. Check the SSH connection and retry."
    return result
