import argparse
import json
from datetime import UTC, datetime

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


def record(conn, instance_state, cpu, gpu, limits, now=None):
    conn.execute(
        "insert into processing.worker_status (id,instance_state,cpu_percent,gpu_percent,activity,measured_utc) "
        "values (1,%s,%s,%s,%s,%s) on conflict (id) do update set instance_state=excluded.instance_state,"
        "cpu_percent=excluded.cpu_percent,gpu_percent=excluded.gpu_percent,activity=excluded.activity,"
        "measured_utc=excluded.measured_utc",
        (instance_state, cpu, gpu, activity(instance_state, cpu, gpu, limits), now or datetime.now(UTC)),
    )


def read(query):
    rows = query(
        "select instance_state as state,cpu_percent as cpu,gpu_percent as gpu,activity,measured_utc "
        "from processing.worker_status where id=1"
    )
    if not rows:
        return None
    row = rows[0]
    row["measured_utc"] = row["measured_utc"].isoformat()
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
