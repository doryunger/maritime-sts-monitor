import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

AIS_MATCHES = """
create table if not exists ais_matches (
    scene_id text, site text, hull_id text, ship_id text, is_sat integer, name text, imo text, mmsi text, flag text,
    seen_utc text, report_age_min real, method text, rep_lat real, rep_lon real, proj_lat real, proj_lon real,
    dist_a_m real, dist_b_m real, limit_m real, ais_length_m real, ais_width_m real, meas_length_m real,
    length_diff_m real, heading_diff_deg real, speed_kn real, chosen integer, slot text, reasons text
);
"""

FINDINGS = """
drop view if exists findings;
create view findings as
select h.scene_id, h.site, h.hull_id, s.source, s.capture_utc, h.lat, h.lon, h.role, h.verdict as run_verdict,
    f.status as qa_status, f.correct_verdict as qa_correct_verdict, f.note as qa_note, f.updated_utc as qa_updated_utc,
    case
        when f.status = 'wrong_verdict' then coalesce(f.correct_verdict, 'unresolved')
        when f.status = 'is_single' then 'single'
        when f.status = 'is_moving' then 'moving'
        when f.status = 'is_pair' then 'pair'
        when f.status in ('bad_geometry', 'other') then 'excluded'
        else h.verdict
    end as verdict
from hulls h join scenes s using (scene_id, site)
left join qa_flags f on f.hull_id = h.scene_id || '__' || h.hull_id
where coalesce(h.role, '') not in ('duplicate', 'hull_b_box');
"""

SCHEMA = """
create table if not exists runs (
    run_id integer primary key autoincrement,
    started_utc text, ended_utc text, args text, hot_spot text, window_start text, window_end text,
    sources text, rules_sha1 text, commit_hash text, status text
);
create table if not exists scenes (
    scene_id text, site text, source text, capture_utc text, pass_id text, status text, stage text,
    attempts integer default 0, error text, detector_s real, counts text, data text, folder text,
    updated_utc text, run_id integer,
    primary key (scene_id, site)
);
create table if not exists raw_detections (
    scene_id text, site text, idx integer, col integer, row integer, lat real, lon real, score real,
    feature text,
    primary key (scene_id, site, idx)
);
create table if not exists hulls (
    scene_id text, site text, hull_id text, lat real, lon real, ai2_length_m real, ai2_width_m real,
    ai2_heading real, verdict text, role text, record text,
    primary key (scene_id, site, hull_id)
);
create table if not exists outputs (
    path text primary key, s3_uri text, created_utc text, capture_date text, site text, source text,
    n_rows integer, n_pairs integer, n_possible integer, scene_ids text, sha256 text, uploaded_utc text,
    run_id integer
);
create table if not exists captures (
    finding_id text primary key, local_path text, s3_uri text, format text
);
create table if not exists qa_flags (
    hull_id text primary key, scene_id text, source text, verdict text, lat real, lon real, status text,
    correct_verdict text, note text, reviewer text, updated_utc text, imported_utc text
);
create table if not exists events (
    event_id integer primary key autoincrement, time_utc text, run_id integer, level text, scene_id text,
    message text
);
"""

IGNORED_ROLES = ("duplicate", "hull_b_box")
HEADER_KEYS = ("scene", "source", "site", "time")
VERDICTS = ("pair", "possible_pair", "single", "moving", "unmeasured")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dump(value) -> str:
    return json.dumps(value, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def verdict_counts(hulls: list[dict]) -> dict:
    return dict(Counter(h["verdict"] for h in hulls if h.get("role") not in IGNORED_ROLES))


class Store:
    def __init__(self, path: Path, root: Path | None = None):
        self.path = Path(path)
        self.root = Path(root) if root else self.path.parent
        self.db = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma busy_timeout=60000")
        self.db.executescript(SCHEMA + AIS_MATCHES + FINDINGS)
        self.depth = 0

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        if self.depth:
            yield
            return
        self.db.execute("begin immediate")
        self.depth = 1
        try:
            yield
        except BaseException:
            self.depth = 0
            self.db.execute("rollback")
            raise
        self.depth = 0
        self.db.execute("commit")

    def rel(self, path: Path) -> str:
        return Path(path).relative_to(self.root).as_posix()

    def start_run(self, args: dict, hot_spot: str, window: tuple[str, str], sources: str, rules_sha1: str, commit: str) -> int:
        with self.tx():
            cur = self.db.execute(
                "insert into runs (started_utc, args, hot_spot, window_start, window_end, sources, rules_sha1, commit_hash, status) values (?,?,?,?,?,?,?,?,?)",
                (now(), dump(args), hot_spot, window[0], window[1], sources, rules_sha1, commit, "running"),
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str) -> None:
        with self.tx():
            self.db.execute("update runs set ended_utc=?, status=? where run_id=?", (now(), status, run_id))

    def event(self, run_id: int | None, level: str, scene_id: str | None, message: str) -> None:
        with self.tx():
            self.db.execute("insert into events (time_utc, run_id, level, scene_id, message) values (?,?,?,?,?)", (now(), run_id, level, scene_id, message))

    def scene(self, scene_id: str, site: str) -> dict | None:
        row = self.db.execute("select * from scenes where scene_id=? and site=?", (scene_id, site)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["counts"] = json.loads(d["counts"]) if d["counts"] else None
        d["data"] = json.loads(d["data"]) if d["data"] else None
        return d

    def scenes(self) -> list[dict]:
        rows = self.db.execute("select scene_id, site from scenes order by capture_utc, scene_id").fetchall()
        return [self.scene(r["scene_id"], r["site"]) for r in rows]

    def n_scenes(self) -> int:
        return self.db.execute("select count(*) from scenes").fetchone()[0]

    def begin_scene(self, run_id: int, scene: dict, folder: Path, pass_id: str | None) -> None:
        with self.tx():
            self.db.execute(
                "insert into scenes (scene_id, site, source, capture_utc, pass_id, status, stage, attempts, folder, updated_utc, run_id) values (?,?,?,?,?,?,?,?,?,?,?) "
                "on conflict(scene_id, site) do update set attempts=attempts+1, status='running', stage='detect', error=null, updated_utc=excluded.updated_utc, run_id=excluded.run_id",
                (scene["scene"], scene["site"], scene["source"], scene["time"], pass_id, "running", "detect", 1, self.rel(folder), now(), run_id),
            )

    def save_detected(self, scene: dict, features: list[dict], counts: dict, detector_s: float, data: dict) -> None:
        key = (scene["scene"], scene["site"])
        with self.tx():
            self.db.execute("delete from raw_detections where scene_id=? and site=?", key)
            rows = []
            for i, f in enumerate(features):
                p = f["properties"]
                rows.append((*key, i, p.get("col"), p.get("row"), p.get("latitude"), p.get("longitude"), p.get("score"), dump(f)))
            self.db.executemany("insert into raw_detections values (?,?,?,?,?,?,?,?,?)", rows)
            self.db.execute(
                "update scenes set status='detected', stage='check', error=null, detector_s=?, counts=?, data=?, updated_utc=? where scene_id=? and site=?",
                (detector_s, dump(counts), dump(data), now(), *key),
            )

    def save_checked(self, scene: dict, hulls: list[dict], pass_id: str | None = None) -> dict:
        key = (scene["scene"], scene["site"])
        with self.tx():
            counts = self.scene(*key)["counts"] | {"checked": len(hulls), "verdicts": verdict_counts(hulls)}
            self.db.execute("delete from hulls where scene_id=? and site=?", key)
            self.db.executemany(
                "insert into hulls values (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        *key,
                        h.get("det_id") or h.get("obj_id"),
                        h["lat"],
                        h["lon"],
                        h.get("ai2_length_m"),
                        h.get("ai2_width_m"),
                        h.get("ai2_heading"),
                        h["verdict"],
                        h.get("role"),
                        dump({k: v for k, v in h.items() if k not in HEADER_KEYS}),
                    )
                    for h in hulls
                ],
            )
            self.db.execute(
                "update scenes set status='checked', stage='done', error=null, counts=?, pass_id=coalesce(?, pass_id), updated_utc=? where scene_id=? and site=?",
                (dump(counts), pass_id, now(), *key),
            )
        return counts

    def fail(self, scene: dict, message: str) -> None:
        with self.tx():
            self.db.execute(
                "update scenes set status=case when status='running' then 'failed' else status end, error=?, updated_utc=? where scene_id=? and site=?",
                (message, now(), scene["scene"], scene["site"]),
            )

    def hulls(self, scene_id: str, site: str) -> list[dict]:
        out = []
        for r in self.db.execute("select * from hulls where scene_id=? and site=? order by rowid", (scene_id, site)):
            rec = json.loads(r["record"])
            out.append(rec | {"lat": r["lat"], "lon": r["lon"], "verdict": r["verdict"], "role": r["role"]})
        return out


def parse_time(scene_id: str, source: str) -> str:
    stamp = scene_id[17:32] if source == "s1" else scene_id.split("_")[2]
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def import_folder(folder: Path) -> str:
    folder = folder.resolve()
    store = Store(folder / "state.db")
    if store.n_scenes():
        raise SystemExit(f"{store.path} already holds {store.n_scenes()} scenes; refusing to import")
    run_id = store.start_run({"import": str(folder)}, "", ("", ""), "", "", "")
    n = 0
    for stage1 in sorted(folder.glob("scenes/*/*/stage1.json")):
        sf = stage1.parent
        source = sf.parent.name
        info = load_json(stage1)
        checks = load_json(sf / "checks.json")
        scene = {
            "scene": sf.name,
            "source": source,
            "site": (checks or {}).get("site", ""),
            "time": (checks or {}).get("time") or parse_time(sf.name, source),
        }
        pass_id = (checks or {}).get("pass")
        store.begin_scene(run_id, scene, sf, pass_id)
        detector_s = info.pop("detector_s")
        data = {k: load_json(sf / f"{k}.json") for k in ("dets", "index", "objects", "windows")}
        store.save_detected(scene, load_json(sf / "features.json") or [], info, detector_s, {k: v for k, v in data.items() if v is not None})
        if checks:
            store.save_checked(scene, checks["hulls"], pass_id)
        n += 1
    store.finish_run(run_id, "imported")
    store.close()
    return f"imported {n} scenes into {folder / 'state.db'}"


def summary(folder: Path) -> list[str]:
    store = Store(folder / "state.db")
    lines = []
    total = Counter()
    secs = 0.0
    scenes = store.scenes()
    for s in scenes:
        c = s["counts"] or {}
        v = c.get("verdicts", {})
        kept = c.get("dets", {}).get("kept", 0)
        total.update(v)
        total["kept"] += kept
        total["checked"] += c.get("checked", 0)
        secs += s["detector_s"] or 0
        counts = " ".join(f"{k} {v.get(k, 0)}" for k in VERDICTS)
        lines.append(f"{s['source']} {s['capture_utc'][:16]} {s['scene_id'][:40]} {s['status']}: kept {kept}, checked {c.get('checked', 0)}, {counts}, detector {s['detector_s'] or 0:.0f} s")
    counts = " ".join(f"{k} {total[k]}" for k in VERDICTS)
    lines.append(f"total {len(scenes)} scenes: kept {total['kept']}, checked {total['checked']}, {counts}, detector {secs:.0f} s")
    store.close()
    return lines


QA_COLUMNS = ("hull_id", "scene_id", "source", "verdict", "lat", "lon", "status", "correct_verdict", "note", "reviewer", "updated_utc", "imported_utc")


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


def upsert_flags(folder: Path, flags: list[dict]) -> int:
    stamp = now()
    rows = [tuple(d.get("imported_utc") or stamp if k == "imported_utc" else d.get(k) for k in QA_COLUMNS) for d in flags]
    store = Store(folder / "state.db", folder)
    with store.tx():
        store.db.executemany(f"insert or replace into qa_flags values ({','.join('?' * len(QA_COLUMNS))})", rows)
    store.close()
    return len(rows)


def import_qa(folder: Path, source: Path) -> int:
    return upsert_flags(folder, read_flags(source))


def final_verdicts(folder: Path) -> dict[str, str]:
    store = Store(folder / "state.db", folder)
    out = {f"{r['scene_id']}__{r['hull_id']}": r["verdict"] for r in store.db.execute("select scene_id, hull_id, verdict from findings")}
    store.close()
    return out


def qa_flags(folder: Path) -> list[dict]:
    store = Store(folder / "state.db", folder)
    rows = [dict(r) for r in store.db.execute(f"select {','.join(QA_COLUMNS)} from qa_flags order by scene_id, hull_id")]
    store.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.watch.state")
    parser.add_argument("command", choices=("import", "summary", "qa-import"))
    parser.add_argument("folder")
    parser.add_argument("flags", nargs="?")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    folder = Path(args.folder)
    if args.command == "import":
        print(import_folder(folder))
    elif args.command == "qa-import":
        print(f"imported {import_qa(folder, Path(args.flags))} QA flags into {folder / 'state.db'}")
    else:
        print("\n".join(summary(folder)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
