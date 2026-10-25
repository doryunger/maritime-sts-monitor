import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from maritime_tip_cue import pg
from maritime_tip_cue.watch import registry
from maritime_tip_cue.watch.pgutil import finite, isoed, jsonb, now

IGNORED_ROLES = ("duplicate", "hull_b_box")
HEADER_KEYS = ("scene", "source", "site", "time")
VERDICTS = ("pair", "possible_pair", "single", "unmeasured")
QA_COLUMNS = ("hull_id", "scene_id", "source", "verdict", "lat", "lon", "site", "hull", "status", "correct_verdict", "note", "reviewer", "updated_utc", "imported_utc")
HULL_COLUMNS = ("run_key", "scene_id", "site", "hull_id", "lat", "lon", "ai2_length_m", "ai2_width_m", "ai2_heading", "verdict", "role", "record")
FLAG_MATCH = (
    "select h.scene_id, h.site, h.hull_id as hull, s.source, h.verdict, h.lat, h.lon from review.hulls h "
    "join review.scenes s on s.run_key = h.run_key and s.scene_id = h.scene_id and s.site = h.site "
    "where h.run_key = %s and (h.scene_id || '__' || h.hull_id = %s or h.scene_id || '__' || h.hull_id || '__' || h.site = %s) "
    "and coalesce(h.role, '') not in ('duplicate', 'hull_b_box')"
)


def verdict_counts(hulls: list[dict]) -> dict:
    return dict(Counter("single" if h["verdict"] == "moving" else h["verdict"] for h in hulls if h.get("role") not in IGNORED_ROLES))


def connect() -> psycopg.Connection:
    conn = pg.connect()
    conn.commit()
    conn.autocommit = True
    conn.row_factory = dict_row
    return conn


class Store:
    def __init__(self, run_key: int, folder: str, run_no: int | None = None):
        self.run_key = run_key
        self.run_no = run_no
        self.folder = folder
        self.path = registry.runs_root() / folder
        self.root = self.path
        self._db = None

    @classmethod
    def from_row(cls, row: dict) -> "Store":
        return cls(row["run_key"], row["folder"], row["run_no"])

    @classmethod
    def create(cls, origin: str = "legacy", attempt_id: str | None = None, job_id: str | None = None) -> "Store":
        store = cls.from_row(registry.new_run(origin, attempt_id, job_id))
        store.path.mkdir(parents=True, exist_ok=True)
        return store

    @classmethod
    def open(cls, run_no: int) -> "Store":
        return cls.from_row(registry.get(run_no))

    @classmethod
    def for_attempt(cls, attempt_id: str) -> "Store | None":
        row = registry.by_attempt(attempt_id)
        return cls.from_row(row) if row else None

    @property
    def db(self) -> psycopg.Connection:
        if self._db is None or self._db.closed or self._db.broken:
            self._db = connect()
        return self._db

    def close(self) -> None:
        if self._db is not None and not self._db.closed:
            self._db.close()
        self._db = None

    def tx(self):
        return self.db.transaction()

    def rel(self, path: Path) -> str:
        return Path(path).relative_to(self.root).as_posix()

    def start_run(self, args: dict, hot_spot: str, window: tuple[str, str], sources: str, rules_sha1: str, commit: str) -> int:
        with self.tx():
            self.db.execute("select 1 from review.runs where run_key = %s for update", (self.run_key,))
            number = self.db.execute("select coalesce(max(invocation_id), 0) + 1 as n from review.run_invocations where run_key = %s", (self.run_key,)).fetchone()["n"]
            self.db.execute(
                "insert into review.run_invocations (run_key, invocation_id, started_utc, args, hot_spot, window_start, window_end, sources, rules_sha1, commit_hash, status) "
                "values (%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,'running')",
                (self.run_key, number, jsonb(args), hot_spot, window[0], window[1], sources, rules_sha1, commit),
            )
        return number

    def finish_run(self, invocation_id: int, status: str) -> None:
        self.db.execute("update review.run_invocations set ended_utc = now(), status = %s where run_key = %s and invocation_id = %s", (status, self.run_key, invocation_id))

    def invocations(self) -> list[dict]:
        rows = self.db.execute("select * from review.run_invocations where run_key = %s order by invocation_id", (self.run_key,)).fetchall()
        return [isoed(r) for r in rows]

    def event(self, invocation_id: int | None, level: str, scene_id: str | None, message: str) -> None:
        self.db.execute(
            "insert into review.events (run_key, event_id, time_utc, invocation_id, level, scene_id, message) "
            "select %s, coalesce(max(event_id), 0) + 1, now(), %s, %s, %s, %s from review.events where run_key = %s",
            (self.run_key, invocation_id, level, scene_id, message, self.run_key),
        )

    def scene(self, scene_id: str, site: str) -> dict | None:
        row = self.db.execute("select * from review.scenes where run_key = %s and scene_id = %s and site = %s", (self.run_key, scene_id, site)).fetchone()
        return isoed(row)

    def scenes(self) -> list[dict]:
        rows = self.db.execute("select * from review.scenes where run_key = %s order by capture_utc, scene_id, site", (self.run_key,)).fetchall()
        return [isoed(r) for r in rows]

    def n_scenes(self) -> int:
        return self.db.execute("select count(*) as n from review.scenes where run_key = %s", (self.run_key,)).fetchone()["n"]

    def begin_scene(self, invocation_id: int, scene: dict, folder: Path, pass_id: str | None) -> None:
        self.db.execute(
            "insert into review.scenes (run_key, scene_id, site, source, capture_utc, pass_id, status, stage, attempts, folder, updated_utc, invocation_id) "
            "values (%s,%s,%s,%s,%s,%s,'running','detect',1,%s,now(),%s) "
            "on conflict (run_key, scene_id, site) do update set attempts = review.scenes.attempts + 1, status = 'running', stage = 'detect', "
            "error = null, updated_utc = now(), invocation_id = excluded.invocation_id",
            (self.run_key, scene["scene"], scene["site"], scene["source"], scene["time"], pass_id, self.rel(folder), invocation_id),
        )

    def save_detected(self, scene: dict, features: list[dict], counts: dict, detector_s: float, data: dict) -> None:
        key = (self.run_key, scene["scene"], scene["site"])
        rows = []
        for i, f in enumerate(features):
            p = f["properties"]
            rows.append((*key, i, p.get("col"), p.get("row"), finite(p.get("latitude")), finite(p.get("longitude")), finite(p.get("score")), jsonb(f)))
        with self.tx():
            self.db.execute("delete from review.raw_detections where run_key = %s and scene_id = %s and site = %s", key)
            with self.db.cursor() as cur:
                cur.executemany(
                    "insert into review.raw_detections (run_key, scene_id, site, idx, col, row, lat, lon, score, feature) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    rows,
                )
            self.db.execute(
                "update review.scenes set status = 'detected', stage = 'check', error = null, detector_s = %s, counts = %s, data = %s, updated_utc = now() "
                "where run_key = %s and scene_id = %s and site = %s",
                (finite(detector_s), jsonb(counts), jsonb(data), *key),
            )

    def save_checked(self, scene: dict, hulls: list[dict], pass_id: str | None = None) -> dict:
        key = (self.run_key, scene["scene"], scene["site"])
        rows = [
            (
                *key,
                h.get("det_id") or h.get("obj_id"),
                finite(h["lat"]),
                finite(h["lon"]),
                finite(h.get("ai2_length_m")),
                finite(h.get("ai2_width_m")),
                finite(h.get("ai2_heading")),
                h["verdict"],
                h.get("role"),
                jsonb({k: v for k, v in h.items() if k not in HEADER_KEYS}),
            )
            for h in hulls
        ]
        names = [r[3] for r in rows]
        with self.tx():
            counts = self.scene(scene["scene"], scene["site"])["counts"] | {"checked": len(hulls), "verdicts": verdict_counts(hulls)}
            with self.db.cursor() as cur:
                cur.executemany(
                    f"insert into review.hulls ({', '.join(HULL_COLUMNS)}) values ({', '.join(['%s'] * len(HULL_COLUMNS))}) "
                    "on conflict (run_key, scene_id, site, hull_id) do update set lat = excluded.lat, lon = excluded.lon, "
                    "ai2_length_m = excluded.ai2_length_m, ai2_width_m = excluded.ai2_width_m, ai2_heading = excluded.ai2_heading, "
                    "verdict = excluded.verdict, role = excluded.role, record = excluded.record",
                    rows,
                )
            gone = "run_key = %s and scene_id = %s and site = %s and hull_id <> all(%s)"
            self.db.execute(
                f"delete from review.hulls h where {gone} and not exists "
                "(select 1 from review.qa_flags f where f.run_key = h.run_key and f.scene_id = h.scene_id and f.site = h.site and f.hull = h.hull_id)",
                (*key, names),
            )
            self.db.execute(f"update review.hulls set role = 'duplicate' where {gone}", (*key, names))
            self.db.execute(
                "update review.scenes set status = 'checked', stage = 'done', error = null, counts = %s, pass_id = coalesce(%s, pass_id), updated_utc = now() "
                "where run_key = %s and scene_id = %s and site = %s",
                (jsonb(counts), pass_id, *key),
            )
        return counts

    def fail(self, scene: dict, message: str) -> None:
        self.db.execute(
            "update review.scenes set status = case when status = 'running' then 'failed' else status end, error = %s, updated_utc = now() "
            "where run_key = %s and scene_id = %s and site = %s",
            (message, self.run_key, scene["scene"], scene["site"]),
        )

    def hulls(self, scene_id: str, site: str) -> list[dict]:
        rows = self.db.execute("select * from review.hulls where run_key = %s and scene_id = %s and site = %s order by hull_id", (self.run_key, scene_id, site)).fetchall()
        return [r["record"] | {"lat": r["lat"], "lon": r["lon"], "verdict": r["verdict"], "role": r["role"]} for r in rows]

    def final_verdicts(self) -> dict[str, str]:
        rows = self.db.execute("select scene_id, hull_id, verdict from review.findings where run_key = %s", (self.run_key,)).fetchall()
        return {f"{r['scene_id']}__{r['hull_id']}": r["verdict"] for r in rows}

    def qa_flags(self) -> list[dict]:
        rows = self.db.execute("select * from review.qa_flags where run_key = %s order by scene_id, hull_id", (self.run_key,)).fetchall()
        return [isoed(r) for r in rows]

    def flag_target(self, hull_id: str) -> dict | None:
        found = self.db.execute(FLAG_MATCH, (self.run_key, hull_id, hull_id)).fetchall()
        return found[0] if len(found) == 1 else None

    def save_flag(self, hull_id: str, fields: dict) -> dict | None:
        target = self.flag_target(hull_id)
        if target is None:
            return None
        verdict = "single" if target["verdict"] == "moving" else target["verdict"]
        row = {
            "hull_id": hull_id, "scene_id": target["scene_id"], "source": target["source"], "verdict": verdict,
            "lat": target["lat"], "lon": target["lon"], "site": target["site"], "hull": target["hull"],
            "status": fields.get("status"), "correct_verdict": fields.get("correct_verdict"), "note": fields.get("note"),
            "reviewer": fields.get("reviewer"), "updated_utc": fields.get("updated_utc") or datetime.now(UTC), "imported_utc": fields.get("imported_utc"),
        }
        columns = ["run_key", *QA_COLUMNS]
        updates = ", ".join(f"{c} = excluded.{c}" for c in QA_COLUMNS if c != "hull_id")
        with self.tx():
            self.db.execute(
                f"insert into review.qa_flags ({', '.join(columns)}) values ({', '.join(['%s'] * len(columns))}) "
                f"on conflict (run_key, hull_id) do update set {updates}",
                [self.run_key, *(row[c] for c in QA_COLUMNS)],
            )
        stored = self.db.execute("select * from review.qa_flags where run_key = %s and hull_id = %s", (self.run_key, hull_id)).fetchone()
        return isoed(stored)


def summary(run_no: int) -> list[str]:
    store = Store.open(run_no)
    lines = []
    total = Counter()
    secs = 0.0
    scenes = store.scenes()
    for s in scenes:
        c = s["counts"] or {}
        v = c.get("verdicts", {})
        kept = c.get("dets", {}).get("kept", 0)
        total.update(v)
        total["single"] += v.get("moving", 0)
        total["kept"] += kept
        total["checked"] += c.get("checked", 0)
        secs += s["detector_s"] or 0
        counts = " ".join(f"{k} {v.get(k, 0) + (v.get('moving', 0) if k == 'single' else 0)}" for k in VERDICTS)
        lines.append(f"{s['source']} {s['capture_utc'][:16]} {s['scene_id'][:40]} {s['status']}: kept {kept}, checked {c.get('checked', 0)}, {counts}, detector {s['detector_s'] or 0:.0f} s")
    counts = " ".join(f"{k} {total[k]}" for k in VERDICTS)
    lines.append(f"total {len(scenes)} scenes: kept {total['kept']}, checked {total['checked']}, {counts}, detector {secs:.0f} s")
    store.close()
    return lines


def read_flags(source: Path) -> list[dict]:
    if source.is_file():
        return [d | {"reviewer": d.get("reviewer", d.get("by"))} for d in json.loads(source.read_text(encoding="utf-8"))]
    out = []
    for path in sorted(source.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        d = doc.get("data", doc)
        if "hull_id" in d:
            out.append(d | {"reviewer": d.get("reviewer", d.get("by"))})
    return out


def import_qa(run_no: int, source: Path) -> tuple[int, int]:
    store = Store.open(run_no)
    stamp = now()
    saved = skipped = 0
    try:
        for d in read_flags(source):
            if store.save_flag(d["hull_id"], d | {"imported_utc": d.get("imported_utc") or stamp}) is None:
                skipped += 1
            else:
                saved += 1
    finally:
        store.close()
    return saved, skipped


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor state")
    parser.add_argument("command", choices=("summary", "qa-import"))
    parser.add_argument("run", type=int)
    parser.add_argument("flags", nargs="?")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "qa-import":
        saved, skipped = import_qa(args.run, Path(args.flags))
        print(f"imported {saved} QA flags into run {args.run}, {skipped} matched no hull")
    else:
        print("\n".join(summary(args.run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
