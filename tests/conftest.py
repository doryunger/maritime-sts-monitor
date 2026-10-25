import os

import psycopg
import pytest
from psycopg import conninfo, sql

import maritime_tip_cue.config  # noqa: F401
from maritime_tip_cue import pg

REAL_URL = os.environ.get("STS_TEST_ADMIN_URL")
DEAD_URL = "postgresql://nobody@127.0.0.1:1/none"
PREFIX = "sts_test"


@pytest.fixture(autouse=True)
def isolate_database(monkeypatch):
    monkeypatch.setenv("STS_DATABASE_URL", DEAD_URL)


@pytest.fixture(scope="session")
def pg_test_url():
    name = os.environ.get("STS_TEST_DB_NAME", "sts_test_main")
    if not name.startswith(PREFIX):
        raise RuntimeError(f"STS_TEST_DB_NAME must start with {PREFIX}: tests empty their tables, so they never run on {name}")
    if not REAL_URL:
        pytest.skip("no throwaway Postgres: run these tests through the Ubuntu host runner (see context/agent_brief.md); STS_TEST_ADMIN_URL is never taken from STS_DATABASE_URL so tests cannot reach the live database")
    try:
        admin = psycopg.connect(conninfo.make_conninfo(REAL_URL, dbname="postgres"), autocommit=True, connect_timeout=10)
    except psycopg.OperationalError as error:
        pytest.skip(f"Postgres not reachable: {str(error).splitlines()[0]}")
    with admin:
        if not admin.execute("select 1 from pg_database where datname = %s", (name,)).fetchone():
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = conninfo.make_conninfo(REAL_URL, dbname=name)
    with psycopg.connect(url) as conn:
        pg.init(conn)
    return url


@pytest.fixture
def pgdb(pg_test_url, monkeypatch):
    monkeypatch.setenv("STS_DATABASE_URL", pg_test_url)
    with pg.connect() as conn:
        tables = [
            f"{schema}.{name}"
            for schema, name in conn.execute(
                "select table_schema, table_name from information_schema.tables "
                "where table_schema in ('review', 'processing') and table_type = 'BASE TABLE'"
            )
        ]
        conn.execute("truncate " + ", ".join(tables) + " restart identity cascade")
        conn.execute("alter sequence review.run_no_seq restart")
        conn.commit()
    with pg.connect() as conn:
        yield conn
