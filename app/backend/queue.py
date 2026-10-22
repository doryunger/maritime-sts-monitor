from datetime import UTC, datetime

from app.backend import ledger

STATUS = {"pending": "ready", "dispatched": "ready", "running": "claimed"}


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
                    "superseded_by": job["superseded_by"],
                    "attempts": job["attempts"],
                    "scene_details": [dict(scene, local_runs=[]) for scene in job["scenes"]],
                }
            )
        result["jobs"] = jobs
    except Exception:
        result["error"] = "Could not read the ledger on the control host. Check the SSH connection and retry."
    return result
