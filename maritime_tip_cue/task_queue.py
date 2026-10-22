import json
import os
import socket
import time
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
        "attempt": f"{root}:attempt:",
        "lease": f"{root}:lease:",
        "result": f"{root}:result:",
        "recovering": f"{root}:recovering",
        "failed": f"{root}:failed",
        "events": f"{root}:events",
        "batch": f"{root}:batch",
        "progress": f"{root}:progress:",
    }


CLAIM = """
local id = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[4], 'LIMIT', 0, 1)
if #id == 0 then return nil end
if redis.call('ZREM', KEYS[1], id[1]) == 0 then return nil end
redis.call('SADD', KEYS[2], id[1])
redis.call('SET', KEYS[3] .. id[1], ARGV[1])
local attempt = ARGV[2]
redis.call('SET', KEYS[4] .. id[1], attempt)
redis.call('SET', KEYS[5] .. id[1], ARGV[1], 'EX', ARGV[3])
redis.call(
  'XADD', KEYS[6], 'MAXLEN', '~', 10000, '*', 'type', 'claimed',
  'job_id', id[1], 'attempt_id', attempt, 'owner', ARGV[1]
)
return id[1]
"""

CLAIM_ID = """
if redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 1 then return nil end
if redis.call('SISMEMBER', KEYS[9], ARGV[1]) == 1 then return nil end
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[5]) then return nil end
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then return nil end
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SET', KEYS[4] .. ARGV[1], ARGV[2])
redis.call('SET', KEYS[6] .. ARGV[1], ARGV[3])
redis.call('SET', KEYS[7] .. ARGV[1], ARGV[2], 'EX', ARGV[4])
redis.call(
  'XADD', KEYS[8], 'MAXLEN', '~', 10000, '*', 'type', 'claimed',
  'job_id', ARGV[1], 'attempt_id', ARGV[3], 'owner', ARGV[2]
)
return ARGV[1]
"""

RESTORE = """
redis.call('SET', KEYS[3], ARGV[2])
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return 0 end
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 0 end
if redis.call('SISMEMBER', KEYS[5], ARGV[1]) == 1 then return 0 end
if redis.call('SISMEMBER', KEYS[6], ARGV[1]) == 1 then return 0 end
redis.call('ZADD', KEYS[4], ARGV[3], ARGV[1])
return 1
"""

SYNC_RETRY_WAIT = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 0 end
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return 0 end
redis.call('SREM', KEYS[3], ARGV[1])
redis.call('SREM', KEYS[4], ARGV[1])
redis.call('DEL', KEYS[5] .. ARGV[1], KEYS[6] .. ARGV[1], KEYS[7] .. ARGV[1])
redis.call('ZADD', KEYS[8], ARGV[2], ARGV[1])
return 1
"""

HEARTBEAT = """
if redis.call('GET', KEYS[1] .. ARGV[1]) ~= ARGV[2] then return 0 end
if redis.call('GET', KEYS[2] .. ARGV[1]) ~= ARGV[3] then return 0 end
return redis.call('EXPIRE', KEYS[3] .. ARGV[1], ARGV[4])
"""

SAVE_RESULT = """
if redis.call('GET', KEYS[1] .. ARGV[1]) ~= ARGV[2] then return 0 end
if redis.call('GET', KEYS[2] .. ARGV[1]) ~= ARGV[3] then return 0 end
if redis.call('EXISTS', KEYS[3] .. ARGV[1]) == 0 then return 0 end
redis.call('SET', KEYS[4] .. ARGV[1] .. ':' .. ARGV[3], ARGV[4])
redis.call(
  'XADD', KEYS[5], 'MAXLEN', '~', 10000, '*', 'type', 'result',
  'job_id', ARGV[1], 'attempt_id', ARGV[3], 'status', ARGV[5]
)
return 1
"""

EXPIRE_LEASES = """
local ids = redis.call('SMEMBERS', KEYS[1])
local expired = 0
for _, id in ipairs(ids) do
  local owner = redis.call('GET', KEYS[2] .. id)
  local attempt = redis.call('GET', KEYS[3] .. id)
  if owner and attempt and redis.call('EXISTS', KEYS[4] .. id) == 0 then
    local result = KEYS[5] .. id .. ':' .. attempt
    if redis.call('EXISTS', result) == 0 then
      redis.call('SADD', KEYS[6], id)
      redis.call(
        'XADD', KEYS[7], 'MAXLEN', '~', 10000, '*', 'type', 'lease_expired',
        'job_id', id, 'attempt_id', attempt, 'owner', owner
      )
      redis.call('SREM', KEYS[1], id)
      redis.call('DEL', KEYS[2] .. id)
      expired = expired + 1
    end
  end
end
return expired
"""

FINALIZE = """
local owner = redis.call('GET', KEYS[1] .. ARGV[1])
local recovering = redis.call('SISMEMBER', KEYS[3], ARGV[1])
local current_attempt = redis.call('GET', KEYS[4] .. ARGV[1])
if owner ~= ARGV[2] and (recovering == 0 or current_attempt ~= ARGV[2]) then return 0 end
if ARGV[5] ~= '' and current_attempt and current_attempt ~= ARGV[5] then return 0 end
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('SREM', KEYS[3], ARGV[1])
redis.call('DEL', KEYS[1] .. ARGV[1], KEYS[4] .. ARGV[1], KEYS[5] .. ARGV[1])
if ARGV[5] ~= '' then redis.call('DEL', KEYS[9] .. ARGV[1] .. ':' .. ARGV[5]) end
if ARGV[3] == 'succeeded' then
  redis.call('SADD', KEYS[6], ARGV[1])
  redis.call('SREM', KEYS[7], ARGV[1])
  redis.call('ZREM', KEYS[8], ARGV[1])
elseif ARGV[3] == 'retry' then
  redis.call('SREM', KEYS[6], ARGV[1])
  redis.call('SREM', KEYS[7], ARGV[1])
  redis.call('ZADD', KEYS[8], ARGV[4], ARGV[1])
else
  redis.call('SREM', KEYS[6], ARGV[1])
  redis.call('SADD', KEYS[7], ARGV[1])
  redis.call('ZREM', KEYS[8], ARGV[1])
end
return 1
"""


def claim(job_id=None, owner=None):
    redis = client()
    k = keys()
    owner = owner or f"{socket.gethostname()}:{uuid.uuid4().hex}"
    from maritime_tip_cue.config import settings

    cfg = settings()["worker"]["queue"]
    attempt_id = uuid.uuid4().hex
    lease = int(cfg["lease_seconds"])
    now_ms = int(time.time() * 1000)
    if job_id:
        job_id = redis.eval(
            CLAIM_ID,
            9,
            k["pending"],
            k["inflight"],
            k["completed"],
            k["owner"],
            k["recovering"],
            k["attempt"],
            k["lease"],
            k["events"],
            k["failed"],
            job_id,
            owner,
            attempt_id,
            lease,
            now_ms,
        )
    else:
        job_id = redis.eval(
            CLAIM,
            6,
            k["pending"],
            k["inflight"],
            k["owner"],
            k["attempt"],
            k["lease"],
            k["events"],
            owner,
            attempt_id,
            lease,
            now_ms,
        )
    if not job_id:
        return None
    value = redis.get(k["manifest"] + job_id)
    if value is None:
        redis.eval(
            FINALIZE,
            9,
            k["owner"],
            k["inflight"],
            k["recovering"],
            k["attempt"],
            k["lease"],
            k["completed"],
            k["failed"],
            k["pending"],
            k["result"],
            job_id,
            owner,
            "failed",
            now_ms,
            attempt_id,
        )
        raise KeyError(f"Redis queue manifest missing for {job_id}")
    manifest = json.loads(value)
    manifest["_queue_attempt_id"] = attempt_id
    return manifest


def heartbeat(job_id, owner, attempt_id):
    redis = client()
    k = keys()
    from maritime_tip_cue.config import settings

    cfg = settings()["worker"]["queue"]
    renewed = redis.eval(
        HEARTBEAT,
        3,
        k["owner"],
        k["attempt"],
        k["lease"],
        job_id,
        owner,
        attempt_id,
        int(cfg["lease_seconds"]),
    )
    return bool(renewed)


def save_result(job_id, owner, receipt):
    redis = client()
    k = keys()
    attempt_id = receipt.get("attempt_id")
    value = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    saved = redis.eval(
        SAVE_RESULT,
        5,
        k["owner"],
        k["attempt"],
        k["lease"],
        k["result"],
        k["events"],
        job_id,
        owner,
        attempt_id,
        value,
        receipt.get("status", "unknown"),
    )
    if not saved:
        raise RuntimeError("Redis task lease was lost before result publication")


def fail_with_result(job_id, owner, manifest, error):
    receipt = {
        "schema_version": 1,
        "job_id": job_id,
        "attempt_id": manifest.get("_queue_attempt_id"),
        "manifest_sha256": job_id,
        "status": "failed",
        "run_no": None,
        "started_utc": datetime.now(UTC).isoformat(),
        "finished_utc": datetime.now(UTC).isoformat(),
        "artifacts": [],
        "scene_counts": {"checked": 0, "failed": len(manifest.get("scenes", []))},
        "error": f"{type(error).__name__}: {error}",
    }
    save_result(job_id, owner, receipt)


def requeue_stale():
    redis = client()
    k = keys()
    return redis.eval(
        EXPIRE_LEASES,
        7,
        k["inflight"],
        k["owner"],
        k["attempt"],
        k["lease"],
        k["result"],
        k["recovering"],
        k["events"],
    )


def restore(jobs, completed):
    redis = client()
    k = keys()
    for job_id, manifest in jobs.items():
        redis.eval(
            RESTORE,
            6,
            k["inflight"],
            k["completed"],
            k["manifest"] + job_id,
            k["pending"],
            k["failed"],
            k["recovering"],
            job_id,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            int(time.time() * 1000),
        )
    if jobs:
        redis.sadd(k["batch"], *jobs.keys())
    for job_id in completed:
        redis.sadd(k["completed"], job_id)
        redis.zrem(k["pending"], job_id)


def finalize(job_id, owner_or_attempt, status, available=None, attempt_id=""):
    redis = client()
    k = keys()
    due = int(available.timestamp() * 1000) if available else int(time.time() * 1000)
    return bool(
        redis.eval(
            FINALIZE,
            9,
            k["owner"],
            k["inflight"],
            k["recovering"],
            k["attempt"],
            k["lease"],
            k["completed"],
            k["failed"],
            k["pending"],
            k["result"],
            job_id,
            owner_or_attempt,
            status,
            due,
            attempt_id,
        )
    )


def sync_terminal(completed, failed):
    redis = client()
    k = keys()
    with redis.pipeline(transaction=True) as pipe:
        for job_id in completed:
            pipe.sadd(k["completed"], job_id)
            pipe.srem(k["failed"], job_id)
            pipe.srem(k["inflight"], job_id)
            pipe.srem(k["recovering"], job_id)
            pipe.zrem(k["pending"], job_id)
            pipe.delete(k["owner"] + job_id, k["attempt"] + job_id, k["lease"] + job_id)
        for job_id in failed:
            pipe.sadd(k["failed"], job_id)
            pipe.srem(k["completed"], job_id)
            pipe.srem(k["inflight"], job_id)
            pipe.srem(k["recovering"], job_id)
            pipe.zrem(k["pending"], job_id)
            pipe.delete(k["owner"] + job_id, k["attempt"] + job_id, k["lease"] + job_id)
        pipe.execute()


def sync_retry_wait(jobs):
    redis = client()
    k = keys()
    for job_id, available_ms in jobs.items():
        redis.eval(
            SYNC_RETRY_WAIT,
            8,
            k["inflight"],
            k["recovering"],
            k["completed"],
            k["failed"],
            k["owner"],
            k["attempt"],
            k["lease"],
            k["pending"],
            job_id,
            available_ms,
        )


def list_jobs():
    redis = client()
    k = keys()
    rows = []
    now_ms = int(time.time() * 1000)
    for job_id, score in redis.zrange(k["pending"], 0, -1, withscores=True):
        value = redis.get(k["manifest"] + job_id)
        if value:
            manifest = json.loads(value)
            rows.append(
                {
                    "job_key": job_id,
                    "job_id": job_id,
                    "site": manifest["site"]["name"],
                    "scenes": len(manifest["scenes"]),
                    "status": "ready" if score <= now_ms else "retry_wait",
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
    for job_id in sorted(redis.smembers(k["failed"])):
        value = redis.get(k["manifest"] + job_id)
        if value:
            manifest = json.loads(value)
            rows.append(
                {
                    "job_key": job_id,
                    "job_id": job_id,
                    "site": manifest["site"]["name"],
                    "scenes": len(manifest["scenes"]),
                    "status": "failed",
                }
            )
    return rows


def ready_ids():
    return client().zrangebyscore(keys()["pending"], "-inf", int(time.time() * 1000))


def counts():
    redis = client()
    k = keys()
    now_ms = int(time.time() * 1000)
    return {
        "ready": redis.zcount(k["pending"], "-inf", now_ms),
        "retry_wait": redis.zcount(k["pending"], f"({now_ms}", "+inf"),
        "claimed": redis.scard(k["inflight"]),
        "completed": redis.scard(k["completed"]),
        "failed": redis.scard(k["failed"]),
    }


def save_progress(job_id, scenes):
    redis = client()
    key = keys()["progress"] + job_id
    with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        if scenes:
            pipe.hset(key, mapping={scene_id: json.dumps(value, sort_keys=True) for scene_id, value in scenes.items()})
        pipe.expire(key, 7 * 86400)
        pipe.execute()


def read_progress(job_id):
    values = client().hgetall(keys()["progress"] + job_id)
    return {scene_id: json.loads(value) for scene_id, value in values.items()}


def drop(job_ids):
    if not job_ids:
        return
    redis = client()
    k = keys()
    with redis.pipeline(transaction=True) as pipe:
        for job_id in job_ids:
            pipe.zrem(k["pending"], job_id)
            for name in ("failed", "inflight", "recovering", "completed"):
                pipe.srem(k[name], job_id)
        pipe.execute()


def clear_failed(job_ids):
    if not job_ids:
        return
    redis = client()
    k = keys()
    with redis.pipeline(transaction=True) as pipe:
        for job_id in job_ids:
            pipe.srem(k["failed"], job_id)
            pipe.srem(k["inflight"], job_id)
            pipe.srem(k["recovering"], job_id)
        pipe.execute()
