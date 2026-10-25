import json
import math
from datetime import UTC, datetime

from psycopg.types.json import Jsonb


def clean(value):
    if hasattr(value, "item") and not isinstance(value, (dict, list, tuple, str)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def finite(value):
    return None if isinstance(value, float) and not math.isfinite(value) else value


def jsonb(value) -> Jsonb | None:
    return None if value is None else Jsonb(clean(value), dumps=lambda v: json.dumps(v, allow_nan=False))


def iso(value):
    if not isinstance(value, datetime):
        return value
    value = value.astimezone(UTC)
    if value.microsecond:
        text = value.strftime("%Y-%m-%dT%H:%M:%S.%f")
        text = text[:-3] if text.endswith("000") else text
        return text + "Z"
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def isoed(row: dict | None, *keys: str) -> dict | None:
    if row is None:
        return None
    return {k: (iso(v) if k in keys or isinstance(v, datetime) else v) for k, v in row.items()}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
