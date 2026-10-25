import threading
import time
from contextlib import closing

from maritime_tip_cue import catalogue

TTL_SECONDS = 20
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}


def fetch() -> dict:
    with _lock:
        if _cache["value"] is not None and time.monotonic() - _cache["at"] < TTL_SECONDS:
            return _cache["value"]
        with closing(catalogue.connect()) as db:
            value = catalogue.ledger(db, catalogue.read_progress())
        _cache.update(at=time.monotonic(), value=value)
        return value
