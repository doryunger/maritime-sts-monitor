import argparse
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from maritime_tip_cue import hotspots, passes
from maritime_tip_cue.config import ROOT, settings
from maritime_tip_cue.sts import rules as rules_mod


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat()


def manifest_core(manifest):
    return {key: manifest[key] for key in ("schema_version", "site", "scenes", "rules", "rules_sha256")}


def validate_manifest(manifest):
    if manifest["schema_version"] != 1 or not manifest["scenes"]:
        raise ValueError("Unsupported or empty scene manifest")
    if manifest["rules_sha256"] != digest(manifest["rules"]):
        raise ValueError("Manifest rule checksum mismatch")
    if manifest["job_id"] != digest(manifest_core(manifest)):
        raise ValueError("Manifest identity mismatch")
    if any(scene["site"] != manifest["site"]["name"] for scene in manifest["scenes"]):
        raise ValueError("Manifest scene/site mismatch")
    return manifest


def make_manifest(site, scenes, raw_rules):
    core = {
        "schema_version": 1,
        "site": site,
        "scenes": sorted(scenes, key=lambda row: (row["source"], row["scene"])),
        "rules": raw_rules,
        "rules_sha256": digest(raw_rules),
    }
    return {**core, "job_id": digest(core), "created_utc": utc_now()}


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            scan_id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, ended_utc TEXT,
            status TEXT NOT NULL, summary_json TEXT
        );
        CREATE TABLE IF NOT EXISTS channels (
            site TEXT NOT NULL, source TEXT NOT NULL, baseline_utc TEXT NOT NULL,
            PRIMARY KEY (site, source)
        );
        CREATE TABLE IF NOT EXISTS observations (
            site TEXT NOT NULL, source TEXT NOT NULL, scene TEXT NOT NULL,
            version TEXT NOT NULL, processing_group TEXT NOT NULL,
            first_discovered_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
            row_json TEXT NOT NULL, PRIMARY KEY (site, source, scene, version)
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, site TEXT NOT NULL, source TEXT NOT NULL,
            created_utc TEXT NOT NULL, manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL, status TEXT NOT NULL,
            dispatched_utc TEXT, completed_utc TEXT, error TEXT, receipt_json TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0, available_utc TEXT, current_attempt_id TEXT
        );
        CREATE TABLE IF NOT EXISTS job_attempts (
            attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
            owner TEXT NOT NULL, status TEXT NOT NULL, started_utc TEXT NOT NULL,
            finished_utc TEXT, error TEXT, receipt_json TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_sources (
            scan_id TEXT NOT NULL, site TEXT NOT NULL, source TEXT NOT NULL,
            status TEXT NOT NULL, matched INTEGER NOT NULL, new INTEGER NOT NULL,
            baseline INTEGER NOT NULL, error TEXT, PRIMARY KEY (scan_id, site, source)
        );
    """)
    job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    for name, definition in (
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("available_utc", "TEXT"),
        ("current_attempt_id", "TEXT"),
        ("superseded_by", "TEXT"),
    ):
        if name not in job_columns:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
    return db


@contextmanager
def controller_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serialize_scene(row):
    result = dict(row)
    geometry = result["geometry"]
    result["geometry"] = geometry.__geo_interface__ if hasattr(geometry, "__geo_interface__") else geometry
    return json.loads(canonical_bytes(result))


def record_channel(db, scan_id, site, source, rows, raw_rules, backfill):
    now = utc_now()
    version = digest({"site": site, "rules": raw_rules})
    initial = db.execute("SELECT 1 FROM channels WHERE site=? AND source=?", (site["name"], source)).fetchone() is None
    new, changed_groups, new_scenes = 0, set(), []
    with db:
        for row in rows:
            stored = serialize_scene(row)
            group = passes.processing_group(source, row["scene"])
            cursor = db.execute(
                "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?)",
                (site["name"], source, row["scene"], version, group, now, now, canonical_bytes(stored).decode()),
            )
            if cursor.rowcount:
                new += 1
                new_scenes.append(row["scene"])
                if not initial or backfill:
                    changed_groups.add(group)
            else:
                db.execute(
                    "UPDATE observations SET last_seen_utc=?, row_json=? "
                    "WHERE site=? AND source=? AND scene=? AND version=?",
                    (now, canonical_bytes(stored).decode(), site["name"], source, row["scene"], version),
                )
        for group in sorted(changed_groups):
            members = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT row_json FROM observations WHERE site=? AND source=? "
                    "AND version=? AND processing_group=? ORDER BY scene",
                    (site["name"], source, version, group),
                )
            ]
            manifest = make_manifest(site, members, raw_rules)
            db.execute(
                "INSERT OR IGNORE INTO jobs "
                "(job_id,site,source,created_utc,manifest_json,manifest_sha256,status) VALUES (?,?,?,?,?,?,?)",
                (
                    manifest["job_id"],
                    site["name"],
                    source,
                    now,
                    canonical_bytes(manifest).decode(),
                    manifest["job_id"],
                    "pending",
                ),
            )
        db.execute("INSERT OR IGNORE INTO channels VALUES (?,?,?)", (site["name"], source, now))
        db.execute(
            "INSERT INTO scan_sources VALUES (?,?,?,?,?,?,?,?)",
            (scan_id, site["name"], source, "ok", len(rows), new, int(initial and not backfill), None),
        )
    return {
        "site": site["name"],
        "source": source,
        "status": "ok",
        "matched": len(rows),
        "new": new,
        "baseline": initial and not backfill,
        "scenes": [{key: row.get(key) for key in ("scene", "time", "cloud", "overlap_km2")} for row in rows],
        "new_scenes": new_scenes,
    }


def record_first_s2_scene(db, scan_id, site, rows, raw_rules):
    version = digest({"site": site, "rules": raw_rules})
    initial = db.execute(
        "SELECT 1 FROM channels WHERE site=? AND source='s2'",
        (site["name"],),
    ).fetchone() is None
    if initial:
        return record_channel(db, scan_id, site, "s2", rows, raw_rules, False), None
    selected = None
    for row in rows:
        exists = db.execute(
            "SELECT 1 FROM observations WHERE site=? AND source='s2' AND scene=? AND version=?",
            (site["name"], row["scene"], version),
        ).fetchone()
        if exists is None:
            manifest = make_manifest(site, [serialize_scene(row)], raw_rules)
            prior_job = db.execute("SELECT 1 FROM jobs WHERE job_id=?", (manifest["job_id"],)).fetchone()
            if prior_job is None:
                selected = row
                break
    if selected is None:
        return record_channel(db, scan_id, site, "s2", rows, raw_rules, False), None
    result = record_channel(db, scan_id, site, "s2", [selected], raw_rules, False)
    result["matched"] = len(rows)
    return result, make_manifest(site, [serialize_scene(selected)], raw_rules)["job_id"]


def reconcile_redis(db, redis, s3, bucket, artifacts_prefix, events, max_retries, retry_delay_seconds):
    from maritime_tip_cue import task_queue

    finalized = 0
    errors = []
    for _event_id, event in events:
        job_id = event.get("job_id")
        attempt_id = event.get("attempt_id")
        event_type = event.get("type")
        try:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                continue
            if event_type == "claimed":
                attempt = db.execute(
                    "SELECT status FROM job_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                with db:
                    if attempt is None and row["status"] not in ("succeeded", "failed", "running"):
                        db.execute(
                            "INSERT OR IGNORE INTO job_attempts "
                            "(attempt_id,job_id,attempt_no,owner,status,started_utc) VALUES (?,?,?,?,?,?)",
                            (
                                attempt_id,
                                job_id,
                                row["retry_count"] + 1,
                                event.get("owner", "unknown"),
                                "running",
                                utc_now(),
                            ),
                        )
                        db.execute(
                            "UPDATE jobs SET status='running',current_attempt_id=? WHERE job_id=?",
                            (attempt_id, job_id),
                        )
                continue
            if event_type not in ("result", "lease_expired"):
                continue
            if row["current_attempt_id"] != attempt_id:
                prior_attempt = db.execute(
                    "SELECT status FROM job_attempts WHERE attempt_id=? AND job_id=?",
                    (attempt_id, job_id),
                ).fetchone()
                if prior_attempt is not None and prior_attempt["status"] in ("failed", "stopped", "lease_expired", "succeeded"):
                    if row["status"] in ("retry_wait", "succeeded", "failed"):
                        final_status = {
                            "retry_wait": "retry",
                            "succeeded": "succeeded",
                            "failed": "failed",
                        }[row["status"]]
                        task_queue.finalize(
                            job_id,
                            event.get("owner", attempt_id),
                            final_status,
                            datetime.fromisoformat(row["available_utc"])
                            if row["status"] == "retry_wait" and row["available_utc"]
                            else None,
                            attempt_id,
                        )
                continue
            attempt_row = db.execute("SELECT * FROM job_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt_row is None:
                raise ValueError("Claim event is missing its SQLite attempt record")
            receipt = None
            status = "failed"
            error_text = "Worker stopped without a receipt: heartbeat lease expired"
            if event_type == "result":
                value = redis.get(task_queue.keys()["result"] + job_id + ":" + attempt_id)
                if value is None:
                    raise ValueError("Result event has no attempt receipt")
                receipt = json.loads(value)
                if (
                    receipt.get("schema_version") != 1
                    or receipt.get("job_id") != job_id
                    or receipt.get("attempt_id") != attempt_id
                    or receipt.get("manifest_sha256") != row["manifest_sha256"]
                    or receipt.get("status") not in ("succeeded", "failed", "stopped")
                    or not isinstance(receipt.get("artifacts"), list)
                ):
                    raise ValueError("Invalid Redis attempt receipt")
                status = receipt["status"]
                error_text = receipt.get("error") or "Worker reported failure"
            if status == "succeeded":
                manifest = validate_manifest(json.loads(row["manifest_json"]))
                expected = artifacts_prefix.rstrip("/") + "/" + job_id + "/" + attempt_id + "/"
                if (
                    not isinstance(receipt.get("scene_counts"), dict)
                    or receipt["scene_counts"].get("checked") != len(manifest["scenes"])
                    or receipt["scene_counts"].get("failed") != 0
                    or not receipt["artifacts"]
                ):
                    raise ValueError("Successful Redis task has incomplete scene counts")
                for artifact in receipt["artifacts"]:
                    if (
                        not isinstance(artifact, dict)
                        or not all(key in artifact for key in ("key", "size", "sha256", "kind"))
                        or not isinstance(artifact["key"], str)
                        or not artifact["key"].startswith(expected)
                        or ".." in artifact["key"].split("/")
                        or not isinstance(artifact["size"], int)
                        or isinstance(artifact["size"], bool)
                        or artifact["size"] < 0
                        or not isinstance(artifact["sha256"], str)
                        or len(artifact["sha256"]) != 64
                        or any(char not in "0123456789abcdef" for char in artifact["sha256"])
                        or not isinstance(artifact["kind"], str)
                    ):
                        raise ValueError("Worker artifact is outside its attempt prefix")
                    head = s3.head_object(Bucket=bucket, Key=artifact["key"])
                    if (
                        head.get("ContentLength") != artifact["size"]
                        or head.get("Metadata", {}).get("sha256") != artifact["sha256"]
                    ):
                        raise ValueError("Worker artifact checksum or size mismatch")
                with db:
                    db.execute(
                        "UPDATE job_attempts SET status='succeeded',finished_utc=?,receipt_json=? WHERE attempt_id=?",
                        (utc_now(), canonical_bytes(receipt).decode(), attempt_id),
                    )
                    db.execute(
                        "UPDATE jobs SET status='succeeded',completed_utc=?,error=NULL,receipt_json=?,"
                        "current_attempt_id=NULL WHERE job_id=?",
                        (utc_now(), canonical_bytes(receipt).decode(), job_id),
                    )
                task_queue.finalize(
                    job_id,
                    event.get("owner", attempt_row["owner"]),
                    "succeeded",
                    attempt_id=attempt_id,
                )
            else:
                retry_count = row["retry_count"] + 1
                retry = retry_count <= max_retries
                available = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds) if retry else None
                terminal_status = "retry_wait" if retry else "failed"
                receipt_text = canonical_bytes(receipt).decode() if receipt else None
                with db:
                    db.execute(
                        "UPDATE job_attempts SET status=?,finished_utc=?,error=?,receipt_json=? WHERE attempt_id=?",
                        (
                            "stopped" if event_type == "lease_expired" or status == "stopped" else "failed",
                            utc_now(),
                            error_text,
                            receipt_text,
                            attempt_id,
                        ),
                    )
                    db.execute(
                        "UPDATE jobs SET status=?,retry_count=?,available_utc=?,error=?,receipt_json=?,"
                        "current_attempt_id=NULL WHERE job_id=?",
                        (
                            terminal_status,
                            retry_count,
                            available.isoformat() if available else None,
                            error_text,
                            receipt_text,
                            job_id,
                        ),
                    )
                finalizer = attempt_row["owner"] if event_type == "result" else attempt_id
                task_queue.finalize(
                    job_id,
                    finalizer,
                    "retry" if retry else "failed",
                    available,
                    attempt_id,
                )
            finalized += 1
        except Exception as error:
            errors.append({"phase": "reconcile", "job_id": job_id, "attempt_id": attempt_id, "error": str(error)})
            raise
    return finalized, errors


def scene_keys(manifest_json):
    return frozenset((row["site"], row["source"], row["scene"]) for row in json.loads(manifest_json)["scenes"])


def supersede_jobs(db):
    rows = db.execute("SELECT job_id,status,created_utc,manifest_json FROM jobs").fetchall()
    keys = {row["job_id"]: scene_keys(row["manifest_json"]) for row in rows}
    succeeded = [row for row in rows if row["status"] == "succeeded"]
    covered = {}
    for row in succeeded:
        for key in keys[row["job_id"]]:
            covered.setdefault(key, row["job_id"])
    open_states = ("pending", "dispatched", "retry_wait")
    active = sorted((row for row in rows if row["status"] in open_states), key=lambda r: (r["created_utc"], r["job_id"]))
    newest = {}
    for row in active:
        newest[keys[row["job_id"]]] = row["job_id"]
    running = {
        keys[row["job_id"]]: row["job_id"] for row in rows if row["status"] == "running"
    }
    result = {}
    for row in rows:
        if row["status"] not in open_states + ("failed",):
            continue
        job_keys = keys[row["job_id"]]
        owners = {covered.get(key) for key in job_keys}
        if None not in owners:
            result[row["job_id"]] = sorted(owners)[0]
        elif row["status"] != "failed" and newest[job_keys] != row["job_id"]:
            result[row["job_id"]] = newest[job_keys]
        elif row["status"] == "failed" and (job_keys in newest or job_keys in running):
            result[row["job_id"]] = newest.get(job_keys) or running[job_keys]
    with db:
        for job_id, by in result.items():
            db.execute(
                "UPDATE jobs SET status='superseded',superseded_by=?,current_attempt_id=NULL WHERE job_id=?",
                (by, job_id),
            )
    return result


OPEN_STATES = ("pending", "dispatched", "retry_wait")


def audit(db, redis=None):
    jobs = db.execute("SELECT job_id,status,manifest_json FROM jobs").fetchall()
    by_scene = {}
    for job in jobs:
        for key in scene_keys(job["manifest_json"]):
            by_scene.setdefault(key, []).append((job["job_id"], job["status"]))
    baseline = {
        (row["site"], row["source"]): row["baseline_utc"] for row in db.execute("SELECT * FROM channels")
    }
    seen = {}
    for row in db.execute("SELECT site,source,scene,MIN(first_discovered_utc) first FROM observations GROUP BY 1,2,3"):
        seen[(row["site"], row["source"], row["scene"])] = row["first"]
    states = {}
    violations = []
    for key in sorted(set(seen) | set(by_scene)):
        statuses = [status for _, status in by_scene.get(key, [])]
        if "succeeded" in statuses:
            state = "completed"
        elif "running" in statuses:
            state = "running"
        elif any(status in OPEN_STATES for status in statuses):
            state = "queued"
        elif "failed" in statuses:
            state = "failed"
        elif statuses:
            state = "superseded_only"
        elif key in seen and seen[key] <= baseline.get(key[:2], ""):
            state = "baseline"
        else:
            state = "orphan"
        states[key] = state
        open_jobs = [job_id for job_id, status in by_scene.get(key, []) if status in OPEN_STATES + ("running",)]
        if state == "orphan":
            violations.append({"scene": key, "problem": "observed after baseline but in no job"})
        if state == "superseded_only":
            violations.append({"scene": key, "problem": "every job for it is superseded"})
        if state == "completed" and open_jobs:
            violations.append({"scene": key, "problem": "completed but still in an open job", "jobs": open_jobs})
        if len(open_jobs) > 1:
            violations.append({"scene": key, "problem": "in more than one open job", "jobs": open_jobs})
    summary = {}
    for (site, source, _), state in states.items():
        counts = summary.setdefault(f"{site}/{source}", {})
        counts[state] = counts.get(state, 0) + 1
    result = {"scenes": summary, "violations": violations}
    if redis is not None:
        from maritime_tip_cue import task_queue

        k = task_queue.keys()
        expected = {
            "completed": {j["job_id"] for j in jobs if j["status"] == "succeeded"},
            "failed": {j["job_id"] for j in jobs if j["status"] == "failed"},
            "inflight": {j["job_id"] for j in jobs if j["status"] == "running"},
            "pending": {j["job_id"] for j in jobs if j["status"] in OPEN_STATES},
        }
        actual = {
            "completed": set(redis.smembers(k["completed"])),
            "failed": set(redis.smembers(k["failed"])),
            "inflight": set(redis.smembers(k["inflight"])),
            "pending": set(redis.zrange(k["pending"], 0, -1)),
        }
        drift = {}
        for name in expected:
            extra, missing = sorted(actual[name] - expected[name]), sorted(expected[name] - actual[name])
            if name == "pending":
                missing = [j for j in missing if (job := next(x for x in jobs if x["job_id"] == j)) and job["status"] != "retry_wait"]
            if extra or missing:
                drift[name] = {"in_redis_only": extra, "in_ledger_only": missing}
        result["redis_drift"] = drift
    return result


def ledger(db, progress=None):
    jobs = []
    for row in db.execute(
        "SELECT job_id,site,source,status,created_utc,dispatched_utc,completed_utc,retry_count,available_utc,"
        "superseded_by,error,manifest_json FROM jobs ORDER BY created_utc DESC,job_id"
    ):
        manifest = json.loads(row["manifest_json"])
        attempts = [
            {
                "attempt_id": attempt["attempt_id"],
                "attempt_no": attempt["attempt_no"],
                "verdict": attempt["status"],
                "owner": attempt["owner"],
                "started_utc": attempt["started_utc"],
                "finished_utc": attempt["finished_utc"],
                "error": attempt["error"],
            }
            for attempt in db.execute(
                "SELECT attempt_id,attempt_no,status,owner,started_utc,finished_utc,error FROM job_attempts "
                "WHERE job_id=? ORDER BY attempt_no",
                (row["job_id"],),
            )
        ]
        stopped = [a for a in attempts if a["verdict"] in ("failed", "stopped", "lease_expired")]
        before_requeue = max(0, len(stopped) - row["retry_count"]) if row["status"] in OPEN_STATES else 0
        for attempt in attempts[:before_requeue]:
            attempt["before_requeue"] = True
        moments = [row["created_utc"], row["dispatched_utc"], row["completed_utc"]]
        moments += [a["started_utc"] for a in attempts] + [a["finished_utc"] for a in attempts]
        jobs.append(
            {
                "job_id": row["job_id"],
                "site": row["site"],
                "source": row["source"],
                "status": row["status"],
                "created_utc": row["created_utc"],
                "retry_count": row["retry_count"],
                "available_utc": row["available_utc"],
                "superseded_by": row["superseded_by"],
                "error": row["error"],
                "last_update_utc": max(m for m in moments if m),
                "progress": (progress or {}).get(row["job_id"], {}),
                "scenes": [
                    {"scene_id": scene["scene"], "source": scene["source"], "captured": scene["time"]}
                    for scene in manifest["scenes"]
                ],
                "attempts": attempts,
            }
        )
    return {"jobs": jobs}


def read_progress():
    progress = {}
    try:
        from maritime_tip_cue import task_queue

        redis = task_queue.client()
        for key in redis.scan_iter(task_queue.keys()["progress"] + "*"):
            progress[key.rsplit(":", 1)[1]] = {scene_id: json.loads(value) for scene_id, value in redis.hgetall(key).items()}
    except Exception:
        return {}
    return progress


def ledger_file(path):
    readonly = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    with closing(readonly) as db:
        return ledger(db, read_progress())


def refresh_queue(db):
    from maritime_tip_cue import task_queue

    now = utc_now()
    jobs = {
        row["job_id"]: json.loads(row["manifest_json"])
        for row in db.execute(
            "SELECT job_id,manifest_json FROM jobs WHERE status IN ('pending','dispatched') "
            "OR (status='retry_wait' AND available_utc<=?)",
            (now,),
        )
    }
    completed = {row[0] for row in db.execute("SELECT job_id FROM jobs WHERE status='succeeded'")}
    task_queue.restore(jobs, completed)
    with db:
        db.execute(
            "UPDATE jobs SET status='dispatched',dispatched_utc=coalesce(dispatched_utc,?) "
            "WHERE status='pending' OR (status='retry_wait' AND available_utc<=?)",
            (now, now),
        )
    return len(jobs)


def refresh_one_job(db, job_id):
    from maritime_tip_cue import task_queue

    row = db.execute(
        "SELECT job_id,manifest_json,status,available_utc FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown job ID: {job_id}")
    available = row["status"] in ("pending", "dispatched") or (
        row["status"] == "retry_wait"
        and row["available_utc"] is not None
        and row["available_utc"] <= utc_now()
    )
    if not available:
        raise ValueError(f"Job {job_id} is not ready for dispatch: {row['status']}")
    manifest = json.loads(row["manifest_json"])
    if len(manifest["scenes"]) != 1:
        raise ValueError(f"Job {job_id} contains {len(manifest['scenes'])} scenes; expected exactly one")
    task_queue.restore({job_id: manifest}, set())
    with db:
        db.execute(
            "UPDATE jobs SET status='dispatched',dispatched_utc=coalesce(dispatched_utc,?) "
            "WHERE job_id=? AND (status IN ('pending','dispatched') OR "
            "(status='retry_wait' AND available_utc<=?))",
            (utc_now(), job_id, utc_now()),
        )
    return {"job_id": job_id, "site": manifest["site"]["name"], "scenes": len(manifest["scenes"])}


def pending_jobs(db, single_scene=False, limit=20):
    now = utc_now()
    rows = db.execute(
        "SELECT job_id,site,source,status,manifest_json FROM jobs "
        "WHERE status IN ('pending','dispatched') OR "
        "(status='retry_wait' AND available_utc<=?) ORDER BY created_utc,job_id",
        (now,),
    )
    result = []
    for row in rows:
        manifest = json.loads(row["manifest_json"])
        if single_scene and len(manifest["scenes"]) != 1:
            continue
        result.append(
            {
                "job_id": row["job_id"],
                "site": row["site"],
                "source": row["source"],
                "status": row["status"],
                "scenes": [scene["scene"] for scene in manifest["scenes"]],
            }
        )
        if len(result) >= limit:
            break
    return result


def job_status(db, job_id):
    row = db.execute(
        "SELECT job_id,site,source,status,error,retry_count,available_utc,manifest_json "
        "FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown job ID: {job_id}")
    manifest = json.loads(row["manifest_json"])
    return {
        "job_id": row["job_id"],
        "site": row["site"],
        "source": row["source"],
        "status": row["status"],
        "error": row["error"],
        "retry_count": row["retry_count"],
        "available_utc": row["available_utc"],
        "scenes": [scene["scene"] for scene in manifest["scenes"]],
        "attempts": [
            {
                "attempt_no": attempt["attempt_no"],
                "owner": attempt["owner"],
                "verdict": attempt["status"],
                "started_utc": attempt["started_utc"],
                "finished_utc": attempt["finished_utc"],
                "error": attempt["error"],
            }
            for attempt in db.execute(
                "SELECT attempt_no,owner,status,started_utc,finished_utc,error FROM job_attempts "
                "WHERE job_id=? ORDER BY attempt_no",
                (job_id,),
            )
        ],
    }


def counts(db):
    return {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}


def scan(db, cfg, backfill=False, first_scene=False):
    scan_id, started = uuid.uuid4().hex, utc_now()
    for previous in db.execute("SELECT scan_id,started_utc FROM scans WHERE status='running'").fetchall():
        interrupted = {
            "scan_id": previous["scan_id"],
            "started_utc": previous["started_utc"],
            "ended_utc": started,
            "status": "interrupted",
            "channels": [],
            "errors": [{"phase": "recovery", "error": "Controller stopped before scan completion"}],
        }
        with db:
            db.execute(
                "UPDATE scans SET ended_utc=?,status='interrupted',summary_json=? WHERE scan_id=?",
                (started, canonical_bytes(interrupted).decode(), previous["scan_id"]),
            )
        write_log(cfg, interrupted)
    summary = {
        "scan_id": scan_id,
        "started_utc": started,
        "channels": [],
        "errors": [],
        "backfill": backfill,
        "first_scene": first_scene,
    }
    with db:
        db.execute("INSERT INTO scans (scan_id,started_utc,status) VALUES (?,?,'running')", (scan_id, started))
    try:
        raw_rules = json.loads(rules_mod.RULES_PATH.read_text(encoding="utf-8"))
        rules = {
            group: {key: entry["value"] for key, entry in body.items() if isinstance(entry, dict) and "value" in entry}
            for group, body in raw_rules.items()
            if isinstance(body, dict)
        }
        sites = hotspots.load()
        if not sites:
            raise ValueError("Shared hot-spot list is empty")
        if first_scene:
            prior_scans = db.execute(
                "SELECT summary_json FROM scans WHERE summary_json IS NOT NULL ORDER BY started_utc DESC"
            ).fetchall()
            for prior in prior_scans:
                prior_summary = json.loads(prior["summary_json"])
                prior_channels = prior_summary.get("channels", [])
                if prior_summary.get("first_scene") and prior_channels:
                    previous_site = prior_channels[0]["site"]
                    site_names = [site["name"] for site in sites]
                    if previous_site in site_names:
                        offset = (site_names.index(previous_site) + 1) % len(sites)
                        sites = sites[offset:] + sites[:offset]
                    break
            summary["site_order"] = [site["name"] for site in sites]
        end = datetime.now(UTC)
        start = end - timedelta(days=rules["catalogue"]["lookback_days"])
        summary["window"] = {"start": start.isoformat(), "end": end.isoformat()}
        summary["rules_sha256"] = digest(raw_rules)
        selected_job_id = None
        for site in sites:
            sources = ("s2",) if first_scene else passes.SOURCES
            for source in sources:
                channel_started = utc_now()
                try:
                    rows = passes.find(start, end, [site], rules=rules, sources=[source])
                    if first_scene:
                        detail, selected_job_id = record_first_s2_scene(db, scan_id, site, rows, raw_rules)
                    else:
                        detail = record_channel(db, scan_id, site, source, rows, raw_rules, backfill)
                    summary["channels"].append(detail)
                except Exception as error:
                    detail = {"site": site["name"], "source": source, "status": "failed", "error": str(error)}
                    summary["channels"].append(detail)
                    summary["errors"].append(detail)
                    with db:
                        db.execute(
                            "INSERT OR REPLACE INTO scan_sources VALUES (?,?,?,?,?,?,?,?)",
                            (scan_id, site["name"], source, "failed", 0, 0, 0, str(error)),
                        )
                summary["channels"][-1].update({"started_utc": channel_started, "ended_utc": utc_now()})
                if selected_job_id:
                    summary["selected_job_id"] = selected_job_id
                    break
                if first_scene and summary["errors"]:
                    break
            if selected_job_id:
                break
            if first_scene and summary["errors"]:
                break
    except Exception as error:
        summary["errors"].append({"phase": "scan", "error": str(error)})
    summary["ended_utc"] = utc_now()
    summary["status"] = "failed" if summary["errors"] else "ok"
    summary["jobs"] = counts(db)
    for channel in summary["channels"]:
        channel["jobs"] = {
            row["status"]: row["n"]
            for row in db.execute(
                "SELECT status,COUNT(*) n FROM jobs WHERE site=? AND source=? GROUP BY status",
                (channel["site"], channel["source"]),
            )
        }
    with db:
        db.execute(
            "UPDATE scans SET ended_utc=?,status=?,summary_json=? WHERE scan_id=?",
            (summary["ended_utc"], summary["status"], canonical_bytes(summary).decode(), scan_id),
        )
    write_log(cfg, summary)
    return summary


def write_log(cfg, summary):
    log = Path(cfg["log_path"])
    log = log if log.is_absolute() else ROOT / log
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(canonical_bytes(summary).decode() + "\n")


def status(db):
    latest = db.execute("SELECT summary_json FROM scans ORDER BY started_utc DESC LIMIT 1").fetchone()
    result = {"last_scan": json.loads(latest[0]) if latest and latest[0] else None, "jobs": counts(db), "sites": []}
    for site in hotspots.load():
        channels = []
        for source in passes.SOURCES:
            observation = db.execute(
                "SELECT COUNT(DISTINCT scene) n,MAX(last_seen_utc) latest FROM observations WHERE site=? AND source=?",
                (site["name"], source),
            ).fetchone()
            jobs = {
                row["status"]: row["n"]
                for row in db.execute(
                    "SELECT status,COUNT(*) n FROM jobs WHERE site=? AND source=? GROUP BY status",
                    (site["name"], source),
                )
            }
            channels.append(
                {
                    "source": source,
                    "observed_scenes": observation["n"],
                    "last_seen_utc": observation["latest"],
                    "jobs": jobs,
                }
            )
        result["sites"].append({"site": site["name"], "channels": channels})
    return result


def requeue_failed(db, job_id=None):
    from maritime_tip_cue import task_queue

    query = "SELECT job_id FROM jobs WHERE status='failed'" + (" AND job_id=?" if job_id else "")
    ids = [row[0] for row in db.execute(query, (job_id,) if job_id else ())]
    with db:
        for failed_id in ids:
            db.execute(
                "UPDATE jobs SET status='pending',retry_count=0,available_utc=NULL,error=NULL,"
                "current_attempt_id=NULL WHERE job_id=?",
                (failed_id,),
            )
    task_queue.clear_failed(ids)
    return {"requeued": ids}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("scan", "scan-one", "status", "jobs", "job-status", "redis-requeue-stale", "requeue-failed", "audit", "ledger"))
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--single-scene", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.single_scene and args.command != "jobs":
        parser.error("--single-scene is only valid with jobs")
    if args.job_id and args.command not in ("job-status", "requeue-failed"):
        parser.error("--job-id is only valid with job-status and requeue-failed")
    if args.command == "job-status" and not args.job_id:
        parser.error("job-status requires --job-id")
    if args.command == "scan-one" and args.backfill:
        parser.error("scan-one does not support backfill")
    if args.command == "redis-requeue-stale":
        from maritime_tip_cue import task_queue

        print(json.dumps({"requeued": task_queue.requeue_stale()}))
        return 0
    cfg = settings()["catalogue"]
    path = Path(cfg["database"])
    path = path if path.is_absolute() else ROOT / path
    if args.command == "ledger":
        print(json.dumps(ledger_file(path), sort_keys=True))
        return 0
    with controller_lock(path), closing(connect(path)) as db:
        if args.command in ("scan", "scan-one"):
            result = scan(db, cfg, args.backfill, args.command == "scan-one")
        elif args.command == "status":
            result = status(db)
        elif args.command == "jobs":
            result = pending_jobs(db, args.single_scene, args.limit)
        elif args.command == "job-status":
            result = job_status(db, args.job_id)
        elif args.command == "audit":
            from maritime_tip_cue import task_queue

            result = audit(db, task_queue.client())
        elif args.command == "requeue-failed":
            result = requeue_failed(db, args.job_id)
        else:
            result = status(db)
    print(json.dumps(result, sort_keys=True))
    return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
