import json
import os
import socket
import uuid
from datetime import UTC, datetime
from functools import cache

from redis import Redis

from maritime_tip_cue.config import env


@cache
def client():
    return Redis.from_url(env("STS_REDIS_URL"), decode_responses=True, socket_connect_timeout=5, socket_timeout=10)


def prefix():
    return os.environ.get("STS_REDIS_PREFIX", "sts:tasks")


def keys():
    root = prefix()
    return {
        "pending": f"{root}:pending",
        "inflight": f"{root}:inflight",
        "completed": f"{root}:completed",
        "manifest": f"{root}:manifest:",
        "owner": f"{root}:owner:",
        "result": f"{root}:result:",
        "events": f"{root}:events",
        "batch": f"{root}:batch",
    }


def refresh(jobs):
    redis = client()
    k = keys()
    inflight = redis.smembers(k["inflight"])
    completed = redis.smembers(k["completed"])
    pipe = redis.pipeline(transaction=True)
    pipe.delete(k["pending"])
    pipe.delete(k["batch"])
    pipe.sadd(k["batch"], *jobs.keys()) if jobs else None
    for job_id, manifest in jobs.items():
        pipe.set(k["manifest"] + job_id, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    for job_id in jobs:
        if job_id not in inflight and job_id not in completed:
            pipe.zadd(k["pending"], {job_id: 0})
    pipe.execute()


def add(job_id, manifest):
    redis = client()
    k = keys()
    redis.set(k["manifest"] + job_id, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    if not redis.sismember(k["inflight"], job_id) and not redis.sismember(k["completed"], job_id):
        redis.zadd(k["pending"], {job_id: 0})


CLAIM = """
local id = redis.call('ZRANGE', KEYS[1], 0, 0)
if #id == 0 then return nil end
if redis.call('ZREM', KEYS[1], id[1]) == 0 then return nil end
redis.call('SADD', KEYS[2], id[1])
redis.call('SET', KEYS[3] .. id[1], ARGV[1])
return id[1]
"""

CLAIM_ID = """
if redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 1 then return nil end
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then return nil end
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SET', KEYS[4] .. ARGV[1], ARGV[2])
return ARGV[1]
"""

RESTORE = """
redis.call('SET', KEYS[3], ARGV[2])
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return 0 end
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 0 end
redis.call('ZADD', KEYS[4], 0, ARGV[1])
return 1
"""

REQUEUE_ALL = """
local ids = redis.call('SMEMBERS', KEYS[1])
for _, id in ipairs(ids) do
  redis.call('ZADD', KEYS[2], 0, id)
  redis.call('DEL', KEYS[3] .. id)
end
redis.call('DEL', KEYS[1])
return #ids
"""


def claim(job_id=None, owner=None):
    redis = client()
    k = keys()
    owner = owner or f"{socket.gethostname()}:{uuid.uuid4().hex}"
    if job_id:
        job_id = redis.eval(CLAIM_ID, 4, k["pending"], k["inflight"], k["completed"], k["owner"], job_id, owner)
    else:
        job_id = redis.eval(CLAIM, 3, k["pending"], k["inflight"], k["owner"], owner)
    if not job_id:
        return None
    value = redis.get(k["manifest"] + job_id)
    if value is None:
        fail(job_id, owner)
        raise KeyError(f"Redis queue manifest missing for {job_id}")
    return json.loads(value)


def acknowledge(job_id, owner):
    redis = client()
    k = keys()
    if redis.get(k["owner"] + job_id) != owner:
        raise RuntimeError("Redis task claim owner changed before acknowledgment")
    with redis.pipeline(transaction=True) as pipe:
        pipe.srem(k["inflight"], job_id)
        pipe.sadd(k["completed"], job_id)
        pipe.delete(k["owner"] + job_id)
        pipe.zrem(k["pending"], job_id)
        pipe.execute()


def save_result(job_id, receipt):
    redis = client()
    value = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    with redis.pipeline(transaction=True) as pipe:
        pipe.set(keys()["result"] + job_id, value)
        pipe.xadd(keys()["events"], {"job_id": job_id, "status": receipt.get("status", "unknown")}, maxlen=10000, approximate=True)
        pipe.execute()


def fail_with_result(job_id, owner, manifest, error):
    receipt = {
        "schema_version": 1,
        "job_id": job_id,
        "manifest_sha256": job_id,
        "status": "failed",
        "run_no": None,
        "started_utc": datetime.now(UTC).isoformat(),
        "finished_utc": datetime.now(UTC).isoformat(),
        "artifacts": [],
        "scene_counts": {"checked": 0, "failed": len(manifest.get("scenes", []))},
        "error": f"{type(error).__name__}: {error}",
    }
    try:
        save_result(job_id, receipt)
    finally:
        fail(job_id, owner)


def fail(job_id, owner):
    redis = client()
    k = keys()
    if redis.get(k["owner"] + job_id) != owner:
        return
    with redis.pipeline(transaction=True) as pipe:
        pipe.srem(k["inflight"], job_id)
        pipe.delete(k["owner"] + job_id)
        pipe.zadd(k["pending"], {job_id: 0})
        pipe.execute()


def release_interrupted(job_id, owner):
    fail(job_id, owner)


def requeue_stale():
    redis = client()
    k = keys()
    return redis.eval(REQUEUE_ALL, 3, k["inflight"], k["pending"], k["owner"])


def restore(jobs, completed):
    redis = client()
    k = keys()
    for job_id, manifest in jobs.items():
        redis.eval(
            RESTORE,
            4,
            k["inflight"],
            k["completed"],
            k["manifest"] + job_id,
            k["pending"],
            job_id,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        )
    if jobs:
        redis.sadd(k["batch"], *jobs.keys())
    for job_id in completed:
        redis.sadd(k["completed"], job_id)
        redis.zrem(k["pending"], job_id)


def list_jobs():
    redis = client()
    k = keys()
    rows = []
    for job_id in sorted(redis.zrange(k["pending"], 0, -1)):
        value = redis.get(k["manifest"] + job_id)
        if value:
            manifest = json.loads(value)
            rows.append(
                {
                    "job_key": job_id,
                    "job_id": job_id,
                    "site": manifest["site"]["name"],
                    "scenes": len(manifest["scenes"]),
                    "status": "ready",
                }
            )
    for job_id in sorted(redis.smembers(k["inflight"])):
        value = redis.get(k["manifest"] + job_id)
        if value:
            manifest = json.loads(value)
            rows.append(
                {
                    "job_key": job_id,
                    "job_id": job_id,
                    "site": manifest["site"]["name"],
                    "scenes": len(manifest["scenes"]),
                    "status": "claimed",
                }
            )
    return rows


def ready_ids():
    return client().zrange(keys()["pending"], 0, -1)
