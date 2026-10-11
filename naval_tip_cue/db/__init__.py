from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from naval_tip_cue.config import env

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(env("DATABASE_URL"), row_factory=dict_row, autocommit=autocommit)


async def connect_async(autocommit: bool = False) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(env("DATABASE_URL"), row_factory=dict_row, autocommit=autocommit)


def init_db() -> None:
    with connect() as conn:
        conn.execute(SCHEMA.read_text())
