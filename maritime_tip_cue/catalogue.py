import argparse
import hashlib
import json
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from maritime_tip_cue import hotspots, passes, pg
from maritime_tip_cue import ledger as attempt_ledger
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


LOCK_KEY = 0x5354534C45444752


def connect():
    db = pg.connect()
    db.commit()
    db.autocommit = True
    return db


def rows(db, sql, params=()):
    with db.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def one(db, sql, params=()):
    found = rows(db, sql, params)
    return found[0] if found else None


@contextmanager
def tx(db):
    with db.transaction():
        yield
    if not db.autocommit:
        db.commit()


def jsonb(value):
    return Jsonb(value, dumps=lambda item: canonical_bytes(item).decode())


def iso(value):
    return value.isoformat() if value is not None else None


@contextmanager
def controller_lock(db):
    if not db.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
        raise BlockingIOError("Another catalogue controller holds the ledger lock")
    try:
        yield
    finally:
        if not db.closed and not db.broken:
            db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def serialize_scene(row):
    result = dict(row)
    geometry = result["geometry"]
    result["geometry"] = geometry.__geo_interface__ if hasattr(geometry, "__geo_interface__") else geometry
    return json.loads(canonical_bytes(result))


def insert_job(db, manifest, site, source, created, scan_id):
    inserted = db.execute(
        "INSERT INTO processing.jobs (job_id,site,source,created_utc,manifest_json,manifest_sha256,status,scan_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,'pending',%s) ON CONFLICT (job_id) DO NOTHING",
        (manifest["job_id"], site, source, created, jsonb(manifest), manifest["job_id"], scan_id),
    ).rowcount
    if inserted:
        for scene in manifest["scenes"]:
            db.execute(
                "INSERT INTO processing.job_scenes (job_id,site,source,scene) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (manifest["job_id"], scene["site"], scene["source"], scene["scene"]),
            )
    return bool(inserted)


def record_channel(db, scan_id, site, source, rows_found, raw_rules, backfill):
    now = datetime.now(UTC)
    version = digest({"site": site, "rules": raw_rules})
    initial = one(db, "SELECT 1 FROM processing.channels WHERE site=%s AND source=%s", (site["name"], source)) is None
    new, changed_groups, new_scenes = 0, set(), []
    with tx(db):
        for row in rows_found:
            stored = serialize_scene(row)
            group = passes.processing_group(source, row["scene"])
            inserted = db.execute(
                "INSERT INTO processing.observations (site,source,scene,version,processing_group,first_discovered_utc,"
                "last_seen_utc,row_json,scan_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (site["name"], source, row["scene"], version, group, now, now, jsonb(stored), scan_id),
            ).rowcount
            if inserted:
                new += 1
                new_scenes.append(row["scene"])
                if not initial or backfill:
                    changed_groups.add(group)
            else:
                db.execute(
                    "UPDATE processing.observations SET last_seen_utc=%s, row_json=%s "
                    "WHERE site=%s AND source=%s AND scene=%s AND version=%s",
                    (now, jsonb(stored), site["name"], source, row["scene"], version),
                )
        for group in sorted(changed_groups):
            members = [
                member["row_json"]
                for member in rows(
                    db,
                    "SELECT row_json FROM processing.observations WHERE site=%s AND source=%s "
                    "AND version=%s AND processing_group=%s ORDER BY scene",
                    (site["name"], source, version, group),
                )
            ]
            insert_job(db, make_manifest(site, members, raw_rules), site["name"], source, now, scan_id)
        db.execute(
            "INSERT INTO processing.channels (site,source,baseline_utc) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (site["name"], source, now),
        )
        db.execute(
            "INSERT INTO processing.scan_sources (scan_id,site,source,status,matched,new,baseline,error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (scan_id, site["name"], source, "ok", len(rows_found), new, int(initial and not backfill), None),
        )
    return {
        "site": site["name"],
        "source": source,
        "status": "ok",
        "matched": len(rows_found),
        "new": new,
        "baseline": initial and not backfill,
        "scenes": [{key: row.get(key) for key in ("scene", "time", "cloud", "overlap_km2")} for row in rows_found],
        "new_scenes": new_scenes,
    }


def record_first_s2_scene(db, scan_id, site, rows_found, raw_rules):
    version = digest({"site": site, "rules": raw_rules})
    initial = one(db, "SELECT 1 FROM processing.channels WHERE site=%s AND source='s2'", (site["name"],)) is None
    if initial:
        return record_channel(db, scan_id, site, "s2", rows_found, raw_rules, False), None
    selected = None
    for row in rows_found:
        exists = one(
            db,
            "SELECT 1 FROM processing.observations WHERE site=%s AND source='s2' AND scene=%s AND version=%s",
            (site["name"], row["scene"], version),
        )
        if exists is None:
            manifest = make_manifest(site, [serialize_scene(row)], raw_rules)
            if one(db, "SELECT 1 FROM processing.jobs WHERE job_id=%s", (manifest["job_id"],)) is None:
                selected = row
                break
    if selected is None:
        return record_channel(db, scan_id, site, "s2", rows_found, raw_rules, False), None
    result = record_channel(db, scan_id, site, "s2", [selected], raw_rules, False)
    result["matched"] = len(rows_found)
    return result, make_manifest(site, [serialize_scene(selected)], raw_rules)["job_id"]


FINISHED_ATTEMPT = ("failed", "stopped", "lease_expired", "succeeded")


def reconcile_redis(db, redis, s3, bucket, artifacts_prefix, events, max_retries, retry_delay_seconds):
    from maritime_tip_cue import task_queue

    finalized = 0
    errors = []
    for _event_id, event in events:
        job_id = event.get("job_id")
        attempt_id = event.get("attempt_id")
        event_type = event.get("type")
        try:
            row = one(db, "SELECT * FROM processing.jobs WHERE job_id=%s", (job_id,))
            if row is None:
                continue
            if event_type == "claimed":
                with tx(db):
                    attempt_ledger.ensure_attempt(db, job_id, attempt_id, event.get("owner", "unknown"))
                continue
            if event_type not in ("result", "lease_expired"):
                continue
            if row["current_attempt_id"] != attempt_id:
                prior_attempt = one(
                    db,
                    "SELECT status FROM processing.job_attempts WHERE attempt_id=%s AND job_id=%s",
                    (attempt_id, job_id),
                )
                if prior_attempt is not None and prior_attempt["status"] in FINISHED_ATTEMPT:
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
                            row["available_utc"] if row["status"] == "retry_wait" else None,
                            attempt_id,
                        )
                continue
            attempt_row = one(db, "SELECT * FROM processing.job_attempts WHERE attempt_id=%s", (attempt_id,))
            if attempt_row is None:
                raise ValueError("Claim event is missing its ledger attempt record")
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
                manifest = validate_manifest(row["manifest_json"])
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
                finished = datetime.now(UTC)
                with tx(db):
                    db.execute(
                        "UPDATE processing.job_attempts SET status='succeeded',finished_utc=%s,receipt_json=%s "
                        "WHERE attempt_id=%s",
                        (finished, jsonb(receipt), attempt_id),
                    )
                    db.execute(
                        "UPDATE processing.jobs SET status='succeeded',completed_utc=%s,error=NULL,receipt_json=%s,"
                        "current_attempt_id=NULL WHERE job_id=%s",
                        (finished, jsonb(receipt), job_id),
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
                receipt_value = jsonb(receipt) if receipt else None
                with tx(db):
                    db.execute(
                        "UPDATE processing.job_attempts SET status=%s,finished_utc=%s,error=%s,receipt_json=%s "
                        "WHERE attempt_id=%s",
                        (
                            "stopped" if event_type == "lease_expired" or status == "stopped" else "failed",
                            datetime.now(UTC),
                            error_text,
                            receipt_value,
                            attempt_id,
                        ),
                    )
                    db.execute(
                        "UPDATE processing.jobs SET status=%s,retry_count=%s,available_utc=%s,error=%s,receipt_json=%s,"
                        "current_attempt_id=NULL WHERE job_id=%s",
                        (terminal_status, retry_count, available, error_text, receipt_value, job_id),
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


def scene_keys(manifest):
    return frozenset((row["site"], row["source"], row["scene"]) for row in manifest["scenes"])


def supersede_jobs(db):
    found = rows(db, "SELECT job_id,status,created_utc,manifest_json FROM processing.jobs")
    keys = {row["job_id"]: scene_keys(row["manifest_json"]) for row in found}
    succeeded = [row for row in found if row["status"] == "succeeded"]
    covered = {}
    for row in succeeded:
        for key in keys[row["job_id"]]:
            covered.setdefault(key, row["job_id"])
    open_states = ("pending", "dispatched", "retry_wait")
    active = sorted(
        (row for row in found if row["status"] in open_states), key=lambda r: (r["created_utc"], r["job_id"])
    )
    newest = {}
    for row in active:
        newest[keys[row["job_id"]]] = row["job_id"]
    running = {
        keys[row["job_id"]]: row["job_id"] for row in found if row["status"] == "running"
    }
    result = {}
    for row in found:
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
    with tx(db):
        for job_id, by in result.items():
            db.execute(
                "UPDATE processing.jobs SET status='superseded',superseded_by=%s,current_attempt_id=NULL "
                "WHERE job_id=%s",
                (by, job_id),
            )
    return result


OPEN_STATES = ("pending", "dispatched", "retry_wait")


def audit(db, redis=None):
    jobs = rows(db, "SELECT job_id,status,manifest_json FROM processing.jobs")
    by_scene = {}
    for job in jobs:
        for key in scene_keys(job["manifest_json"]):
            by_scene.setdefault(key, []).append((job["job_id"], job["status"]))
    baseline = {
        (row["site"], row["source"]): row["baseline_utc"] for row in rows(db, "SELECT * FROM processing.channels")
    }
    seen = {}
    for row in rows(
        db,
        "SELECT site,source,scene,MIN(first_discovered_utc) first FROM processing.observations GROUP BY 1,2,3",
    ):
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
        elif key in seen and seen[key] <= baseline.get(key[:2], datetime.min.replace(tzinfo=UTC)):
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
                waiting = {j["job_id"] for j in jobs if j["status"] == "retry_wait"}
                missing = [j for j in missing if j not in waiting]
            if extra or missing:
                drift[name] = {"in_redis_only": extra, "in_ledger_only": missing}
        result["redis_drift"] = drift
    return result


def ledger(db, progress=None):
    attempts_by_job = {}
    for attempt in rows(
        db,
        "SELECT job_id,attempt_id,attempt_no,status,owner,started_utc,finished_utc,error "
        "FROM processing.job_attempts ORDER BY attempt_no",
    ):
        attempts_by_job.setdefault(attempt["job_id"], []).append(
            {
                "attempt_id": attempt["attempt_id"],
                "attempt_no": attempt["attempt_no"],
                "verdict": attempt["status"],
                "owner": attempt["owner"],
                "started_utc": attempt["started_utc"],
                "finished_utc": attempt["finished_utc"],
                "error": attempt["error"],
            }
        )
    jobs = []
    for row in rows(
        db,
        "SELECT job_id,site,source,status,created_utc,dispatched_utc,completed_utc,retry_count,available_utc,"
        "superseded_by,error,manifest_json->'scenes' AS scenes FROM processing.jobs ORDER BY created_utc DESC,job_id",
    ):
        attempts = attempts_by_job.get(row["job_id"], [])
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
                "created_utc": iso(row["created_utc"]),
                "retry_count": row["retry_count"],
                "available_utc": iso(row["available_utc"]),
                "superseded_by": row["superseded_by"],
                "error": row["error"],
                "last_update_utc": iso(max(m for m in moments if m)),
                "progress": (progress or {}).get(row["job_id"], {}),
                "scenes": [
                    {"scene_id": scene["scene"], "source": scene["source"], "captured": scene["time"]}
                    for scene in row["scenes"]
                ],
                "attempts": [
                    dict(a, started_utc=iso(a["started_utc"]), finished_utc=iso(a["finished_utc"])) for a in attempts
                ],
            }
        )
    return {"jobs": jobs}


def read_progress():
    progress = {}
    try:
        from maritime_tip_cue import task_queue

        redis = task_queue.client()
        for key in redis.scan_iter(task_queue.keys()["progress"] + "*"):
            scenes = redis.hgetall(key).items()
            progress[key.rsplit(":", 1)[1]] = {scene_id: json.loads(value) for scene_id, value in scenes}
    except Exception:
        return {}
    return progress


READY = "status IN ('pending','dispatched') OR (status='retry_wait' AND available_utc<=%s)"


def refresh_queue(db):
    from maritime_tip_cue import task_queue

    now = datetime.now(UTC)
    jobs = {
        row["job_id"]: row["manifest_json"]
        for row in rows(db, f"SELECT job_id,manifest_json FROM processing.jobs WHERE {READY}", (now,))
    }
    completed = {row["job_id"] for row in rows(db, "SELECT job_id FROM processing.jobs WHERE status='succeeded'")}
    task_queue.restore(jobs, completed)
    with tx(db):
        db.execute(
            "UPDATE processing.jobs SET status='dispatched',dispatched_utc=coalesce(dispatched_utc,%s) "
            "WHERE status='pending' OR (status='retry_wait' AND available_utc<=%s)",
            (now, now),
        )
    return len(jobs)


def refresh_one_job(db, job_id):
    from maritime_tip_cue import task_queue

    row = one(
        db,
        "SELECT job_id,manifest_json,status,available_utc FROM processing.jobs WHERE job_id=%s",
        (job_id,),
    )
    if row is None:
        raise ValueError(f"Unknown job ID: {job_id}")
    now = datetime.now(UTC)
    available = row["status"] in ("pending", "dispatched") or (
        row["status"] == "retry_wait" and row["available_utc"] is not None and row["available_utc"] <= now
    )
    if not available:
        raise ValueError(f"Job {job_id} is not ready for dispatch: {row['status']}")
    manifest = row["manifest_json"]
    if len(manifest["scenes"]) != 1:
        raise ValueError(f"Job {job_id} contains {len(manifest['scenes'])} scenes; expected exactly one")
    task_queue.restore({job_id: manifest}, set())
    with tx(db):
        db.execute(
            "UPDATE processing.jobs SET status='dispatched',dispatched_utc=coalesce(dispatched_utc,%s) "
            "WHERE job_id=%s AND (status IN ('pending','dispatched') OR "
            "(status='retry_wait' AND available_utc<=%s))",
            (now, job_id, now),
        )
    return {"job_id": job_id, "site": manifest["site"]["name"], "scenes": len(manifest["scenes"])}


def pending_jobs(db, single_scene=False, limit=20):
    found = rows(
        db,
        f"SELECT job_id,site,source,status,manifest_json FROM processing.jobs WHERE {READY} "
        "ORDER BY created_utc,job_id",
        (datetime.now(UTC),),
    )
    result = []
    for row in found:
        manifest = row["manifest_json"]
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
    row = one(
        db,
        "SELECT job_id,site,source,status,error,retry_count,available_utc,manifest_json "
        "FROM processing.jobs WHERE job_id=%s",
        (job_id,),
    )
    if row is None:
        raise ValueError(f"Unknown job ID: {job_id}")
    return {
        "job_id": row["job_id"],
        "site": row["site"],
        "source": row["source"],
        "status": row["status"],
        "error": row["error"],
        "retry_count": row["retry_count"],
        "available_utc": iso(row["available_utc"]),
        "scenes": [scene["scene"] for scene in row["manifest_json"]["scenes"]],
        "attempts": [
            {
                "attempt_no": attempt["attempt_no"],
                "owner": attempt["owner"],
                "verdict": attempt["status"],
                "started_utc": iso(attempt["started_utc"]),
                "finished_utc": iso(attempt["finished_utc"]),
                "error": attempt["error"],
            }
            for attempt in rows(
                db,
                "SELECT attempt_no,owner,status,started_utc,finished_utc,error FROM processing.job_attempts "
                "WHERE job_id=%s ORDER BY attempt_no",
                (job_id,),
            )
        ],
    }


def counts(db):
    found = rows(db, "SELECT status,COUNT(*) n FROM processing.jobs GROUP BY status")
    return {row["status"]: row["n"] for row in found}


def scan(db, cfg, backfill=False, first_scene=False):
    scan_id, started_at = uuid.uuid4().hex, datetime.now(UTC)
    started = started_at.isoformat()
    for previous in rows(db, "SELECT scan_id,started_utc FROM processing.scans WHERE status='running'"):
        interrupted = {
            "scan_id": previous["scan_id"],
            "started_utc": iso(previous["started_utc"]),
            "ended_utc": started,
            "status": "interrupted",
            "channels": [],
            "errors": [{"phase": "recovery", "error": "Controller stopped before scan completion"}],
        }
        with tx(db):
            db.execute(
                "UPDATE processing.scans SET ended_utc=%s,status='interrupted',summary_json=%s WHERE scan_id=%s",
                (started_at, jsonb(interrupted), previous["scan_id"]),
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
    with tx(db):
        db.execute(
            "INSERT INTO processing.scans (scan_id,started_utc,status) VALUES (%s,%s,'running')", (scan_id, started_at)
        )
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
            prior_scans = rows(
                db, "SELECT summary_json FROM processing.scans WHERE summary_json IS NOT NULL ORDER BY started_utc DESC"
            )
            for prior in prior_scans:
                prior_summary = prior["summary_json"]
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
                    found = passes.find(start, end, [site], rules=rules, sources=[source])
                    if first_scene:
                        detail, selected_job_id = record_first_s2_scene(db, scan_id, site, found, raw_rules)
                    else:
                        detail = record_channel(db, scan_id, site, source, found, raw_rules, backfill)
                    summary["channels"].append(detail)
                except Exception as error:
                    detail = {"site": site["name"], "source": source, "status": "failed", "error": str(error)}
                    summary["channels"].append(detail)
                    summary["errors"].append(detail)
                    with tx(db):
                        db.execute(
                            "INSERT INTO processing.scan_sources "
                            "(scan_id,site,source,status,matched,new,baseline,error) "
                            "VALUES (%s,%s,%s,'failed',0,0,0,%s) ON CONFLICT (scan_id,site,source) DO UPDATE SET "
                            "status=excluded.status,matched=0,new=0,baseline=0,error=excluded.error",
                            (scan_id, site["name"], source, str(error)),
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
    ended_at = datetime.now(UTC)
    summary["ended_utc"] = ended_at.isoformat()
    summary["status"] = "failed" if summary["errors"] else "ok"
    summary["jobs"] = counts(db)
    for channel in summary["channels"]:
        channel["jobs"] = {
            row["status"]: row["n"]
            for row in rows(
                db,
                "SELECT status,COUNT(*) n FROM processing.jobs WHERE site=%s AND source=%s GROUP BY status",
                (channel["site"], channel["source"]),
            )
        }
    with tx(db):
        db.execute(
            "UPDATE processing.scans SET ended_utc=%s,status=%s,summary_json=%s WHERE scan_id=%s",
            (ended_at, summary["status"], jsonb(summary), scan_id),
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
    latest = one(db, "SELECT summary_json FROM processing.scans ORDER BY started_utc DESC LIMIT 1")
    result = {"last_scan": latest["summary_json"] if latest else None, "jobs": counts(db), "sites": []}
    for site in hotspots.load():
        channels = []
        for source in passes.SOURCES:
            observation = one(
                db,
                "SELECT COUNT(DISTINCT scene) n,MAX(last_seen_utc) latest FROM processing.observations "
                "WHERE site=%s AND source=%s",
                (site["name"], source),
            )
            jobs = {
                row["status"]: row["n"]
                for row in rows(
                    db,
                    "SELECT status,COUNT(*) n FROM processing.jobs WHERE site=%s AND source=%s GROUP BY status",
                    (site["name"], source),
                )
            }
            channels.append(
                {
                    "source": source,
                    "observed_scenes": observation["n"],
                    "last_seen_utc": iso(observation["latest"]),
                    "jobs": jobs,
                }
            )
        result["sites"].append({"site": site["name"], "channels": channels})
    return result


def requeue_failed(db, job_id=None):
    from maritime_tip_cue import task_queue

    query = "SELECT job_id FROM processing.jobs WHERE status='failed'" + (" AND job_id=%s" if job_id else "")
    ids = [row["job_id"] for row in rows(db, query, (job_id,) if job_id else ())]
    with tx(db):
        for failed_id in ids:
            db.execute(
                "UPDATE processing.jobs SET status='pending',retry_count=0,available_utc=NULL,error=NULL,"
                "current_attempt_id=NULL WHERE job_id=%s",
                (failed_id,),
            )
    task_queue.clear_failed(ids)
    return {"requeued": ids}


def main():
    parser = argparse.ArgumentParser()
    commands = (
        "scan", "scan-one", "status", "jobs", "job-status", "redis-requeue-stale", "requeue-failed", "audit", "ledger"
    )
    parser.add_argument("command", choices=commands)
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
    if args.command == "ledger":
        with closing(connect()) as db:
            print(json.dumps(ledger(db, read_progress()), sort_keys=True))
        return 0
    with closing(connect()) as db, controller_lock(db):
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
