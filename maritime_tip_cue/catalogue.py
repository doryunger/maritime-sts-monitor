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
from maritime_tip_cue.config import ROOT, env, settings
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
            dispatched_utc TEXT, completed_utc TEXT, error TEXT, receipt_json TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_sources (
            scan_id TEXT NOT NULL, site TEXT NOT NULL, source TEXT NOT NULL,
            status TEXT NOT NULL, matched INTEGER NOT NULL, new INTEGER NOT NULL,
            baseline INTEGER NOT NULL, error TEXT, PRIMARY KEY (scan_id, site, source)
        );
    """)
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


def object_key(prefix, job_id):
    return prefix.rstrip("/") + "/" + job_id + ".json"


def missing_object(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")


def reconcile(db, client, bucket, prefix, artifacts_prefix):
    errors = []
    completed = 0
    for job in db.execute("SELECT * FROM jobs WHERE status IN ('dispatched','failed')").fetchall():
        try:
            try:
                receipt = json.loads(
                    client.get_object(Bucket=bucket, Key=object_key(prefix, job["job_id"]))["Body"].read()
                )
            except Exception as error:
                if missing_object(error):
                    continue
                raise
            if (
                receipt.get("schema_version") != 1
                or receipt.get("job_id") != job["job_id"]
                or receipt.get("manifest_sha256") != job["manifest_sha256"]
                or receipt.get("status") not in ("succeeded", "failed")
            ):
                raise ValueError("Invalid worker receipt identity or status")
            if not isinstance(receipt.get("artifacts"), list):
                raise ValueError("Invalid worker receipt artifacts")
            manifest = validate_manifest(json.loads(job["manifest_json"]))
            if receipt["status"] == "succeeded":
                if (
                    not isinstance(receipt.get("scene_counts"), dict)
                    or receipt["scene_counts"].get("failed") != 0
                    or receipt["scene_counts"].get("checked") != len(manifest["scenes"])
                ):
                    raise ValueError("Successful receipt has incomplete scene counts")
                if not receipt["artifacts"]:
                    raise ValueError("Successful receipt has no artifacts")
            expected_prefix = artifacts_prefix.rstrip("/") + "/" + job["job_id"] + "/"
            for artifact in receipt["artifacts"]:
                if not isinstance(artifact, dict) or not all(
                    key in artifact for key in ("key", "sha256", "size", "kind")
                ):
                    raise ValueError("Invalid worker artifact receipt")
                if (
                    not isinstance(artifact["key"], str)
                    or not artifact["key"].startswith(expected_prefix)
                    or ".." in artifact["key"].split("/")
                ):
                    raise ValueError("Worker artifact is outside its job prefix")
                if (
                    not isinstance(artifact["sha256"], str)
                    or len(artifact["sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in artifact["sha256"])
                ):
                    raise ValueError("Invalid worker artifact checksum")
                if (
                    not isinstance(artifact["size"], int)
                    or isinstance(artifact["size"], bool)
                    or artifact["size"] < 0
                    or not isinstance(artifact["kind"], str)
                ):
                    raise ValueError("Invalid worker artifact size or kind")
                if receipt["status"] == "succeeded":
                    head = client.head_object(Bucket=bucket, Key=artifact["key"])
                    if (
                        head.get("ContentLength") != artifact["size"]
                        or head.get("Metadata", {}).get("sha256") != artifact["sha256"]
                    ):
                        raise ValueError("Worker artifact object checksum or size mismatch")
            receipt_json = canonical_bytes(receipt).decode()
            if job["receipt_json"] == receipt_json:
                continue
            with db:
                db.execute(
                    "UPDATE jobs SET status=?,completed_utc=?,error=?,receipt_json=? WHERE job_id=?",
                    (receipt["status"], utc_now(), receipt.get("error"), receipt_json, job["job_id"]),
                )
            completed += 1
        except Exception as error:
            errors.append({"phase": "reconcile", "job_id": job["job_id"], "error": str(error)})
    return completed, errors


def dispatch(db, client, bucket, prefix):
    dispatched, errors = 0, []
    for job in db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_utc,job_id").fetchall():
        try:
            key = object_key(prefix, job["job_id"])
            manifest = json.loads(job["manifest_json"])
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=canonical_bytes(manifest),
                    ContentType="application/json",
                    Metadata={"sha256": job["manifest_sha256"]},
                    IfNoneMatch="*",
                )
            except Exception as error:
                code = getattr(error, "response", {}).get("Error", {}).get("Code")
                if code not in ("PreconditionFailed", "412"):
                    raise
                existing = validate_manifest(json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read()))
                if existing["job_id"] != job["manifest_sha256"]:
                    raise ValueError("Existing S3 manifest differs from durable outbox")
            with db:
                db.execute(
                    "UPDATE jobs SET status='dispatched', dispatched_utc=?,error=NULL WHERE job_id=?",
                    (utc_now(), job["job_id"]),
                )
            dispatched += 1
        except Exception as error:
            with db:
                db.execute("UPDATE jobs SET error=? WHERE job_id=?", (str(error), job["job_id"]))
            errors.append({"phase": "dispatch", "job_id": job["job_id"], "error": str(error)})
    return dispatched, errors


def counts(db):
    return {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}


def scan(db, cfg, backfill=False):
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
    summary = {"scan_id": scan_id, "started_utc": started, "channels": [], "errors": [], "backfill": backfill}
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
        end = datetime.now(UTC)
        start = end - timedelta(days=rules["catalogue"]["lookback_days"])
        summary["window"] = {"start": start.isoformat(), "end": end.isoformat()}
        summary["rules_sha256"] = digest(raw_rules)
        for site in sites:
            for source in passes.SOURCES:
                channel_started = utc_now()
                try:
                    rows = passes.find(start, end, [site], rules=rules, sources=[source])
                    summary["channels"].append(record_channel(db, scan_id, site, source, rows, raw_rules, backfill))
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
    except Exception as error:
        summary["errors"].append({"phase": "scan", "error": str(error)})
    try:
        client, bucket = hotspots._client(), env("S3_BUCKET_NAME")
        summary["completed"], errors = reconcile(db, client, bucket, cfg["results_prefix"], cfg["artifacts_prefix"])
        summary["errors"].extend(errors)
        summary["dispatched"], errors = dispatch(db, client, bucket, cfg["jobs_prefix"])
        summary["errors"].extend(errors)
    except Exception as error:
        summary["errors"].append({"phase": "queue", "error": str(error)})
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("scan", "status"))
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args()
    cfg = settings()["catalogue"]
    path = Path(cfg["database"])
    path = path if path.is_absolute() else ROOT / path
    with controller_lock(path), closing(connect(path)) as db:
        result = scan(db, cfg, args.backfill) if args.command == "scan" else status(db)
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
