import argparse
import json
from datetime import UTC, datetime, time, timedelta

from maritime_tip_cue import pg
from maritime_tip_cue.config import ROOT

RULES_PATH = ROOT / "rules" / "worker_idle.json"


def load_limits():
    raw = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    return raw["cpu_busy_percent_max"]["value"], raw["gpu_busy_percent_max"]["value"]


def activity(instance_state, cpu, gpu, limits):
    if instance_state != "running":
        return None
    if cpu is None or gpu is None:
        return "unreachable"
    cpu_max, gpu_max = limits
    if gpu >= gpu_max:
        return "detecting"
    if cpu >= cpu_max:
        return "preparing imagery"
    return "idle"


def add_running(conn, start, end):
    cursor = start.astimezone(UTC)
    end = end.astimezone(UTC)
    while cursor < end:
        midnight = datetime.combine(cursor.date() + timedelta(days=1), time(), tzinfo=UTC)
        stop = min(end, midnight)
        conn.execute(
            "insert into processing.worker_uptime (day,seconds) values (%s,%s) "
            "on conflict (day) do update set seconds=processing.worker_uptime.seconds+excluded.seconds",
            (cursor.date(), (stop - cursor).total_seconds()),
        )
        cursor = stop


def month_bounds(today):
    first = today.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following


def record(conn, instance_state, cpu, gpu, limits, now=None):
    now = now or datetime.now(UTC)
    previous = conn.execute("select instance_state,measured_utc from processing.worker_status where id=1 for update").fetchone()
    if previous and previous[0] == "running" and instance_state == "running":
        add_running(conn, previous[1], now)
    conn.execute(
        "insert into processing.worker_status (id,instance_state,cpu_percent,gpu_percent,activity,measured_utc) "
        "values (1,%s,%s,%s,%s,%s) on conflict (id) do update set instance_state=excluded.instance_state,"
        "cpu_percent=excluded.cpu_percent,gpu_percent=excluded.gpu_percent,activity=excluded.activity,"
        "measured_utc=excluded.measured_utc",
        (instance_state, cpu, gpu, activity(instance_state, cpu, gpu, limits), now),
    )


def read(query, today=None):
    rows = query(
        "select instance_state as state,cpu_percent as cpu,gpu_percent as gpu,activity,measured_utc "
        "from processing.worker_status where id=1"
    )
    if not rows:
        return None
    row = rows[0]
    row["measured_utc"] = row["measured_utc"].isoformat()
    first, following = month_bounds(today or datetime.now(UTC).date())
    total = query("select coalesce(sum(seconds),0) as seconds from processing.worker_uptime where day>=%s and day<%s", (first, following))
    row["month_seconds"] = int(total[0]["seconds"])
    return row


def main():
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.worker_status")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("record")
    write.add_argument("--state", required=True)
    write.add_argument("--cpu", type=int)
    write.add_argument("--gpu", type=int)
    args = parser.parse_args()
    with pg.connect() as conn:
        record(conn, args.state, args.cpu, args.gpu, load_limits())
    print(json.dumps({"state": args.state}))


if __name__ == "__main__":
    main()
