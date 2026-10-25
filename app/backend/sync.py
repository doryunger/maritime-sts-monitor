from psycopg.rows import dict_row

from maritime_tip_cue import pg
from maritime_tip_cue.watch.pgutil import iso

FIELDS = "sync_id, requested_utc, requested_by, status, dry_run, started_utc, finished_utc, summary, error"
LOCK_KEY = 7301


def shape(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: iso(v) for k, v in row.items()}


def state(cur) -> dict:
    cur.execute(f"select {FIELDS} from review.sync_runs order by sync_id desc limit 1")
    latest = cur.fetchone()
    cur.execute(f"select {FIELDS} from review.sync_runs where status in ('done', 'failed') order by sync_id desc limit 1")
    finished = cur.fetchone()
    cur.execute(f"select {FIELDS} from review.sync_runs where status in ('pending', 'running') order by sync_id limit 1")
    active = cur.fetchone()
    return {"latest": shape(latest), "active": shape(active), "last_finished": shape(finished)}


def current() -> dict:
    with pg.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        return state(cur)


def request(user: str) -> tuple[bool, dict]:
    with pg.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        cur.execute(f"select {FIELDS} from review.sync_runs where status in ('pending', 'running') order by sync_id limit 1")
        active = cur.fetchone()
        if active is None:
            cur.execute("insert into review.sync_runs (requested_by, status) values (%s, 'pending')", (user,))
        result = state(cur)
        conn.commit()
        return active is None, result
