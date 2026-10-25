import json
import sqlite3
from datetime import UTC, datetime

import pytest

from maritime_tip_cue import pgimport


def test_timestamps_become_aware_utc_and_blank_becomes_null():
    assert pgimport.convert("2026-10-19T20:40:06Z", "timestamp with time zone") == datetime(2026, 10, 19, 20, 40, 6, tzinfo=UTC)
    assert pgimport.convert("2026-10-19T11:33:32.454Z", "timestamp with time zone").microsecond == 454000
    assert pgimport.convert("2026-10-20T09:15:00.123456Z", "timestamp with time zone").microsecond == 123456
    assert pgimport.convert("", "timestamp with time zone") is None
    assert pgimport.convert(None, "timestamp with time zone") is None


def test_an_unreadable_timestamp_stops_the_import_instead_of_becoming_null():
    with pytest.raises(ValueError):
        pgimport.convert("last tuesday", "timestamp with time zone")


def test_flags_and_numbers_are_typed_from_the_sqlite_values():
    assert pgimport.convert(1, "boolean") is True
    assert pgimport.convert(0, "boolean") is False
    assert pgimport.convert(3.0, "integer") == 3
    assert pgimport.convert("2.5", "double precision") == 2.5
    assert pgimport.convert("keep", "text") == "keep"


def test_json_is_validated_and_non_finite_numbers_become_null():
    text = json.dumps({"a": [1, float("nan"), {"b": float("inf")}], "c": "x"})
    assert json.loads(pgimport.convert(text, "jsonb")) == {"a": [1, None, {"b": None}], "c": "x"}
    with pytest.raises(ValueError):
        pgimport.convert("{not json", "jsonb")


def test_runs_from_the_pc_are_legacy_and_the_rest_come_from_jobs():
    assert pgimport.origin_of("legacy-run-9") == "legacy"
    assert pgimport.origin_of("7edea2dfce25-3c1f8770") == "job"


def test_every_sqlite_column_has_a_postgres_column():
    schema = pgimport.pg.SCHEMA
    memory = sqlite3.connect(":memory:")
    memory.executescript(pgimport.LEGACY_SCHEMA)
    pairs = list(pgimport.TABLES) + [("vessel_identities", "vessel_identities")]
    for sqlite_table, pg_table in pairs:
        block = schema.split(f"create table if not exists review.{pg_table} (")[1].split("\n);")[0]
        for _, name, *_ in memory.execute(f"pragma table_info({sqlite_table})").fetchall():
            target = pgimport.RENAMES.get(name, name)
            assert f" {target} " in f" {block.replace(',', ' , ').replace(chr(10), ' ')} ".replace(" , ", " "), (sqlite_table, name)
