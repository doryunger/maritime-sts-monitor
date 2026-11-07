from collections import Counter

from psycopg.rows import dict_row

from maritime_tip_cue import grid_status
from maritime_tip_cue.watch.pgutil import iso

LOCK_KEY = 7302


class RequestLost(Exception):
    pass

FIELDS = "request_id,requested_utc,requested_by,kind,mode,run_no,batch_utc,status,started_utc,heartbeat_utc,finished_utc,error"
FRESH = "now() - coalesce(heartbeat_utc, requested_utc) < make_interval(mins => %s)"


def reader(cur):
    def rows(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchall()

    return rows


def latest_batch(rows):
    return rows(
        "SELECT d.batch_utc, COUNT(*) AS tiles, COUNT(v.tile_id) AS decided, "
        "CASE WHEN bool_or(d.kind<>'regular') THEN 'pairs' ELSE 'regular' END AS mode FROM processing.grid_draws d "
        "LEFT JOIN processing.grid_visits v ON v.tile_id=d.tile_id AND v.round=d.round "
        "WHERE d.batch_utc=(SELECT MAX(batch_utc) FROM processing.grid_draws) GROUP BY d.batch_utc"
    )


def elapsed_seconds(rows, run_no, mode, stale):
    found = rows(
        "SELECT COALESCE(SUM(GREATEST(0, EXTRACT(EPOCH FROM ("
        "CASE WHEN status='running' AND fresh THEN now() "
        "WHEN status='running' THEN COALESCE(heartbeat_utc, started_utc) "
        "WHEN error='interrupted' THEN COALESCE(LEAST(heartbeat_utc, finished_utc), started_utc) "
        "ELSE COALESCE(finished_utc, heartbeat_utc, started_utc) END - started_utc)))),0) AS seconds FROM "
        f"(SELECT *, {FRESH} AS fresh FROM processing.grid_run_requests) r WHERE run_no=%s AND mode=%s AND started_utc IS NOT NULL",
        (stale, run_no, mode),
    )
    return int(found[0]["seconds"])


def state(cur, rules, mode="regular"):
    rows = reader(cur)
    stale = rules["request_stale_minutes"]
    batch = next(iter(latest_batch(rows)), None)
    request = next(iter(rows(f"SELECT {FIELDS},{FRESH} AS fresh FROM processing.grid_run_requests ORDER BY request_id DESC LIMIT 1", (stale,))), None)
    unfinished = batch is not None and batch["decided"] < batch["tiles"]
    active = request is not None and request["status"] in ("pending", "running") and request["fresh"]
    counts = Counter(run["mode"] for run in grid_status.scan_runs(rows, rules))
    if active:
        action, run_no, status, mode = None, request["run_no"], "starting" if request["status"] == "pending" else "running", request["mode"]
    elif unfinished:
        action, mode, status = "continue", batch["mode"], "interrupted"
        run_no = counts[mode]
    else:
        action, run_no, status = "start", counts[mode] + 1, "idle"
    shown = request if request is not None and (active or unfinished) else None
    tracked = batch if batch is not None and (status == "interrupted" or (shown is not None and shown["batch_utc"] == batch["batch_utc"])) else None
    return {
        "action": action,
        "mode": mode,
        "run_no": run_no,
        "label": grid_status.run_label(mode, run_no),
        "status": status,
        "requested_by": shown["requested_by"] if shown else None,
        "elapsed_seconds": elapsed_seconds(rows, run_no, mode, stale),
        "heartbeat_utc": iso(shown["heartbeat_utc"] or shown["requested_utc"]) if shown else None,
        "error": shown["error"] if shown and not active and shown["error"] not in ("interrupted", "not picked up", "stopped") else None,
        "decided": tracked["decided"] if tracked else None,
        "tiles": tracked["tiles"] if tracked else None,
    }


def expire(cur, rules):
    stale = rules["request_stale_minutes"]
    cur.execute(
        "UPDATE processing.grid_run_requests SET status='failed', finished_utc=now(), "
        "error=CASE status WHEN 'pending' THEN 'not picked up' ELSE 'interrupted' END "
        f"WHERE status IN ('pending','running') AND NOT ({FRESH})",
        (stale,),
    )


def request(conn, user, action, rules, mode="regular"):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        expire(cur, rules)
        current = state(cur, rules, mode)
        if current["action"] != action:
            conn.commit()
            return False, current
        if action == "continue":
            batch_utc = next(iter(latest_batch(reader(cur))))["batch_utc"]
        else:
            cur.execute("SELECT now() AS now")
            batch_utc = cur.fetchone()["now"]
        cur.execute(
            "INSERT INTO processing.grid_run_requests (requested_by,kind,mode,run_no,batch_utc) VALUES (%s,%s,%s,%s,%s)",
            (user, action, current["mode"], current["run_no"], batch_utc),
        )
        result = state(cur, rules, mode)
        conn.commit()
        return True, result


def claim(conn, rules):
    with conn.cursor(row_factory=dict_row) as cur:
        expire(cur, rules)
        cur.execute(
            "SELECT request_id FROM processing.grid_run_requests WHERE status='pending' "
            "ORDER BY requested_utc,request_id LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        found = cur.fetchone()
        if found is None:
            conn.commit()
            return None
        cur.execute(
            f"UPDATE processing.grid_run_requests SET status='running', started_utc=now(), heartbeat_utc=now() "
            f"WHERE request_id=%s RETURNING {FIELDS}",
            (found["request_id"],),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def beat(conn, request_id):
    beaten = conn.execute(
        "UPDATE processing.grid_run_requests SET heartbeat_utc=now() WHERE request_id=%s AND status='running'", (request_id,)
    ).rowcount
    conn.commit()
    if not beaten:
        raise RequestLost(f"request {request_id} is no longer running")


def finish(conn, request_id, status, error=None):
    conn.execute(
        "UPDATE processing.grid_run_requests SET status=%s, finished_utc=now(), error=%s WHERE request_id=%s AND status='running'",
        (status, error, request_id),
    )
    conn.commit()
