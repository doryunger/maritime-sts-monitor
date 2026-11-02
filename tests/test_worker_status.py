import pytest

from datetime import UTC, date, datetime

from maritime_tip_cue import worker_status

LIMITS = (5, 5)


@pytest.mark.parametrize(
    "state,cpu,gpu,expected",
    [
        ("stopped", None, None, None),
        ("stopping", 90, 90, None),
        ("running", None, None, "unreachable"),
        ("running", 2, 0, "idle"),
        ("running", 60, 0, "preparing imagery"),
        ("running", 30, 97, "detecting"),
        ("running", 5, 0, "preparing imagery"),
    ],
)
def test_activity_from_the_worker_readings(state, cpu, gpu, expected):
    assert worker_status.activity(state, cpu, gpu, LIMITS) == expected


def test_record_keeps_one_row_and_read_returns_it(pgdb):
    assert worker_status.read(lambda sql, params=(): [dict(r) for r in _rows(pgdb, sql)]) is None
    worker_status.record(pgdb, "running", 60, 0, LIMITS)
    worker_status.record(pgdb, "running", 2, 1, LIMITS)
    pgdb.commit()
    row = worker_status.read(lambda sql, params=(): _rows(pgdb, sql))
    assert (row["state"], row["cpu"], row["gpu"], row["activity"]) == ("running", 2, 1, "idle")
    assert pgdb.execute("select count(*) from processing.worker_status").fetchone()[0] == 1


def _rows(conn, sql):
    cursor = conn.execute(sql)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def test_limits_come_from_the_rules_file():
    assert worker_status.load_limits() == (5, 5)


def _month(pgdb, today):
    return worker_status.read(lambda sql, params=(): _rows_with(pgdb, sql, params), today)["month_seconds"]


def _rows_with(conn, sql, params):
    cursor = conn.execute(sql, params)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def test_running_time_counts_only_between_two_running_checks_and_splits_at_midnight(pgdb):
    at = lambda month, day, hour, minute=0: datetime(2026, month, day, hour, minute, tzinfo=UTC)
    worker_status.record(pgdb, "stopped", None, None, LIMITS, at(10, 31, 20))
    worker_status.record(pgdb, "running", 1, 1, LIMITS, at(10, 31, 21))
    worker_status.record(pgdb, "running", 1, 1, LIMITS, at(10, 31, 23, 30))
    worker_status.record(pgdb, "running", 1, 1, LIMITS, at(11, 1, 1))
    worker_status.record(pgdb, "stopping", None, None, LIMITS, at(11, 1, 1, 3))
    worker_status.record(pgdb, "stopped", None, None, LIMITS, at(11, 1, 1, 6))
    days = dict(pgdb.execute("select day,seconds from processing.worker_uptime").fetchall())
    assert days == {date(2026, 10, 31): 9000 + 1800, date(2026, 11, 1): 3600}
    assert _month(pgdb, date(2026, 10, 15)) == 10800
    assert _month(pgdb, date(2026, 11, 30)) == 3600
    assert _month(pgdb, date(2026, 12, 1)) == 0
