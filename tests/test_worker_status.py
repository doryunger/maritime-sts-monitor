import pytest

from maritime_tip_cue import pg, worker_status

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
