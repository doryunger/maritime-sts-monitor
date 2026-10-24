import time

import fakeredis
import pytest

from maritime_tip_cue import config, task_queue

QUEUE = {"lease_seconds": 900, "heartbeat_seconds": 60, "max_retries": 1, "retry_delay_seconds": 300}


@pytest.fixture
def redis(monkeypatch):
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_queue, "client", lambda: server)
    monkeypatch.setattr(config, "settings", lambda: {"worker": {"queue": QUEUE}})
    return server


def manifest(job_id):
    return {"job_id": job_id, "site": {"name": "site-a"}, "scenes": [{"scene": "s"}]}


def dispatch(job_id):
    task_queue.restore({job_id: manifest(job_id)}, [])


def receipt(job_id, attempt_id, status="succeeded"):
    return {"job_id": job_id, "attempt_id": attempt_id, "status": status}


def test_second_claim_of_same_job_is_refused(redis):
    dispatch("j")
    first = task_queue.claim("j", "w1")
    assert first["_queue_attempt_id"]
    assert task_queue.claim("j", "w2") is None
    assert redis.get(task_queue.keys()["owner"] + "j") == "w1"


def test_heartbeat_requires_owner_and_attempt(redis):
    dispatch("j")
    attempt = task_queue.claim("j", "w1")["_queue_attempt_id"]
    assert task_queue.heartbeat("j", "w1", attempt)
    assert not task_queue.heartbeat("j", "w2", attempt)
    assert not task_queue.heartbeat("j", "w1", "other")


def test_result_from_stale_attempt_is_rejected(redis):
    dispatch("j")
    old = task_queue.claim("j", "w1")["_queue_attempt_id"]
    redis.delete(task_queue.keys()["lease"] + "j")
    assert task_queue.requeue_stale() == 1
    task_queue.finalize("j", old, "retry", attempt_id=old)
    new = task_queue.claim("j", "w2")["_queue_attempt_id"]
    assert new != old
    with pytest.raises(RuntimeError):
        task_queue.save_result("j", "w1", receipt("j", old))
    task_queue.save_result("j", "w2", receipt("j", new))


def test_expired_lease_emits_event_and_marks_recovering(redis):
    dispatch("j")
    attempt = task_queue.claim("j", "w1")["_queue_attempt_id"]
    redis.delete(task_queue.keys()["lease"] + "j")
    assert task_queue.requeue_stale() == 1
    k = task_queue.keys()
    assert redis.sismember(k["recovering"], "j")
    events = [fields for _, fields in redis.xrange(k["events"])]
    assert events[-1]["type"] == "lease_expired" and events[-1]["attempt_id"] == attempt


def test_expired_lease_with_saved_result_is_not_recovered(redis):
    dispatch("j")
    attempt = task_queue.claim("j", "w1")["_queue_attempt_id"]
    task_queue.save_result("j", "w1", receipt("j", attempt))
    redis.delete(task_queue.keys()["lease"] + "j")
    assert task_queue.requeue_stale() == 0


def test_retry_is_not_claimable_until_due(redis):
    dispatch("j")
    attempt = task_queue.claim("j", "w1")["_queue_attempt_id"]
    from datetime import UTC, datetime, timedelta

    task_queue.finalize("j", "w1", "retry", datetime.now(UTC) + timedelta(seconds=300), attempt)
    assert task_queue.claim("j", "w2") is None
    assert task_queue.ready_ids() == []
    assert task_queue.list_jobs()[0]["status"] == "retry_wait"
    redis.zadd(task_queue.keys()["pending"], {"j": int(time.time() * 1000) - 1})
    assert task_queue.claim("j", "w2") is not None


def test_terminal_states_block_further_claims(redis):
    dispatch("j")
    attempt = task_queue.claim("j", "w1")["_queue_attempt_id"]
    assert task_queue.finalize("j", "w1", "failed", attempt_id=attempt)
    k = task_queue.keys()
    assert redis.sismember(k["failed"], "j")
    redis.zadd(k["pending"], {"j": 0})
    assert task_queue.claim("j", "w2") is None


def test_restore_does_not_requeue_active_or_terminal_jobs(redis):
    dispatch("running")
    task_queue.claim("running", "w1")
    dispatch("done")
    attempt = task_queue.claim("done", "w1")["_queue_attempt_id"]
    task_queue.finalize("done", "w1", "succeeded", attempt_id=attempt)
    task_queue.restore({"running": manifest("running"), "done": manifest("done")}, [])
    assert redis.zscore(task_queue.keys()["pending"], "running") is None
    assert redis.zscore(task_queue.keys()["pending"], "done") is None


def test_missing_manifest_fails_claim_and_clears_inflight(redis):
    dispatch("j")
    redis.delete(task_queue.keys()["manifest"] + "j")
    with pytest.raises(KeyError):
        task_queue.claim("j", "w1")
    assert not redis.sismember(task_queue.keys()["inflight"], "j")


def test_progress_is_published_per_scene_and_replaced_on_each_write(redis):
    task_queue.save_progress("j", {"a": {"status": "running", "stage": "detect"}, "b": {"status": "failed"}})
    task_queue.save_progress("j", {"a": {"status": "checked", "stage": None}})
    assert task_queue.read_progress("j") == {"a": {"status": "checked", "stage": None}}
    assert redis.ttl(task_queue.keys()["progress"] + "j") > 0


def test_restore_keeps_the_original_ready_time_so_clock_skew_cannot_hide_a_job(redis):
    dispatch("j")
    first = redis.zscore(task_queue.keys()["pending"], "j")
    time.sleep(0.01)
    task_queue.restore({"j": manifest("j")}, [])
    assert redis.zscore(task_queue.keys()["pending"], "j") == first
