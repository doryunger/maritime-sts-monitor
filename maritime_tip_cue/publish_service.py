import argparse
import json
import sys
import traceback

from psycopg.rows import dict_row

from maritime_tip_cue import pg
from maritime_tip_cue.config import env
from maritime_tip_cue.sts import rules as rules_mod

ACTIVE = ("pending", "running")


def abandon_stale(conn, hours: float) -> int:
    rows = conn.execute(
        "update review.sync_runs set status = 'failed', finished_utc = now(), error = 'abandoned' "
        "where status = 'running' and started_utc < now() - make_interval(secs => %s) returning sync_id",
        (hours * 3600,),
    ).fetchall()
    conn.commit()
    return len(rows)


def claim(conn) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select sync_id from review.sync_runs where status = 'pending' order by requested_utc, sync_id limit 1 for update skip locked"
        )
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        cur.execute(
            "update review.sync_runs set status = 'running', started_utc = now() where sync_id = %s returning *",
            (row["sync_id"],),
        )
        claimed = cur.fetchone()
    conn.commit()
    return claimed


def finish(conn, sync_id: int, status: str, summary: dict | None, error: str | None) -> None:
    conn.execute(
        "update review.sync_runs set status = %s, finished_utc = now(), summary = %s::jsonb, error = %s where sync_id = %s",
        (status, json.dumps(summary) if summary is not None else None, error, sync_id),
    )
    conn.commit()


def execute(conn, row: dict, client, bucket: str, rules: dict, sync) -> str:
    try:
        _, summary = sync(client, bucket, [], not row["dry_run"], row["dry_run"], rules, row["sync_id"])
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        message = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, str) else "".join(traceback.format_exception_only(type(exc), exc)).strip()
        finish(conn, row["sync_id"], "failed", None, message)
        return "failed"
    finish(conn, row["sync_id"], "done", summary, None)
    return "done"


def run_pending(conn=None, client=None, bucket: str | None = None, rules: dict | None = None, sync=None) -> str | None:
    own = conn is None
    conn = conn or pg.connect()
    try:
        rules = rules or rules_mod.load()
        abandon_stale(conn, rules["publish"]["sync_abandon_hours"])
        row = claim(conn)
        if row is None:
            return None
        if sync is None:
            from maritime_tip_cue import publish_sync

            sync = publish_sync.sync_report
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=env("AWS_REGION"))
        return execute(conn, row, client, bucket or env("S3_BUCKET_NAME"), rules, sync)
    finally:
        if own:
            conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor publish_service")
    parser.add_argument("command", choices=["run-pending"])
    parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    outcome = run_pending()
    if outcome:
        print(f"sync {outcome}")
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
