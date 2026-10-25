import psycopg

BLOCKING = ("succeeded", "failed", "running")


def ensure_attempt(conn: psycopg.Connection, job_id: str, attempt_id: str, owner: str) -> bool:
    job = conn.execute("select status, retry_count from processing.jobs where job_id = %s for update", (job_id,)).fetchone()
    if job is None:
        return False
    if conn.execute("select 1 from processing.job_attempts where attempt_id = %s", (attempt_id,)).fetchone():
        return False
    if job[0] in BLOCKING:
        return False
    conn.execute(
        "insert into processing.job_attempts (attempt_id, job_id, attempt_no, owner, status, started_utc) "
        "values (%s, %s, %s, %s, 'running', now()) on conflict (attempt_id) do nothing",
        (attempt_id, job_id, job[1] + 1, owner),
    )
    conn.execute("update processing.jobs set status = 'running', current_attempt_id = %s where job_id = %s", (attempt_id, job_id))
    return True
