import hashlib
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from maritime_tip_cue.config import ROOT, settings
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry, report
from maritime_tip_cue.watch.state import QA_COLUMNS, Store, now, qa_flags, upsert_flags

LISTED = ("pair", "possible_pair", "unmeasured")
SINGLES = 24
FLAGS = ("ok", "wrong_verdict", "bad_geometry", "other")
LABELS = ("is_single", "is_pair")
CORRECT = ("pair", "single")
STALE_MIN = 60

CACHE: dict[int, tuple] = {}
LOCKS: dict[int, threading.Lock] = {}
SCENE_CACHE: dict[int, tuple] = {}
SCENE_LOCK = threading.Lock()


def reviews_dir() -> Path:
    return ROOT / settings()["app"]["reviews"]


def folder(no: int) -> Path:
    return registry.folder_of(no)


def counts(store: Store, column: str) -> dict:
    return {k: n for k, n in store.db.execute(f"select {column}, count(*) from findings group by {column}")}


def status_of(runs: list[dict], last_event: str | None) -> str:
    if not runs:
        return "empty"
    last = runs[-1]
    if last["ended_utc"]:
        return last["status"] or "done"
    if last_event:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last_event.replace("Z", "+00:00"))
        if age.total_seconds() > STALE_MIN * 60:
            return "stopped"
    return "running"


def review_of(status: str, reviewed: int, listed: int, unresolved: int) -> str:
    if status == "running":
        return "waiting for run"
    if status == "failed":
        return "nothing to review"
    if not listed:
        return "nothing to review"
    if not reviewed:
        return "not started"
    if reviewed < listed or unresolved:
        return "in progress"
    return "done"


def summary(row: dict) -> dict:
    path = registry.runs_root() / row["folder"]
    out = {"run": row["run_no"], "created": row["created_utc"], "area": None, "status": "empty"}
    if not (path / "state.db").exists():
        return out
    store = Store(path / "state.db", path)
    try:
        runs = [dict(r) for r in store.db.execute("select * from runs order by run_id")]
        named = [r for r in runs if r["hot_spot"]]
        scenes = {k: n for k, n in store.db.execute("select status, count(*) from scenes group by status")}
        last_event = store.db.execute("select max(time_utc) from events").fetchone()[0]
        run_v, final = counts(store, "run_verdict"), counts(store, "verdict")
        reviewed = store.db.execute("select count(*) from qa_flags").fetchone()[0]
        flagged = store.db.execute("select count(*) from qa_flags f where not exists (select 1 from findings h where h.scene_id || '__' || h.hull_id = f.hull_id and (h.run_verdict in ('pair', 'possible_pair', 'unmeasured')))").fetchone()[0]
        captured = list(store.db.execute("select min(capture_utc), max(capture_utc) from scenes").fetchone())
    finally:
        store.close()
    listed = sum(run_v.get(k, 0) for k in LISTED) + max(min(SINGLES, run_v.get("single", 0)), flagged)
    status = status_of(runs, last_event)
    return out | {
        "area": named[-1]["hot_spot"] if named else None,
        "window": [runs[0]["window_start"], runs[-1]["window_end"]] if runs else None,
        "sources": named[-1]["sources"] if named else None,
        "status": status,
        "review": review_of(status, reviewed, listed, final.get("unresolved", 0)),
        "captured": captured,
        "finished": runs[-1]["ended_utc"] if runs else None,
        "scenes": sum(scenes.values()),
        "scenes_failed": scenes.get("failed", 0),
        "run_verdicts": run_v,
        "verdicts": final,
        "reviewed": reviewed,
        "listed": listed,
    }


def summaries() -> list[dict]:
    return [summary(r) for r in registry.listing()]


def scene_status(status: str, run_status: str) -> str:
    if status == "checked":
        return "done"
    if status == "failed":
        return "failed"
    return "running" if run_status == "running" else "stopped"


def file_signature(path: Path) -> tuple:
    result = [str(path.resolve())]
    for target in (path, path.with_name(path.name + "-wal")):
        try:
            stat = target.stat()
            result.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def read_scene_index(path: Path) -> dict:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("begin")
        invocations = [dict(r) for r in db.execute(
            "select ended_utc, status from runs order by run_id"
        )]
        last_event = db.execute("select max(time_utc) from events").fetchone()[0]
        scenes = [dict(r) for r in db.execute(
            "select scene_id, site, source, capture_utc, status, updated_utc "
            "from scenes order by capture_utc, scene_id"
        )]
        flags = {r[0] for r in db.execute("select hull_id from qa_flags")}
        groups: dict = {}
        for value in db.execute(
            "select h.scene_id, h.site, h.hull_id, f.run_verdict, f.verdict as final_verdict "
            "from hulls h join findings f using (scene_id, site, hull_id) order by h.rowid"
        ):
            hull = dict(value)
            groups.setdefault((hull["scene_id"], hull["site"]), []).append(hull)
    finally:
        db.close()
    hulls, taken = [], set()
    for scene in scenes:
        for hull in groups.get((scene["scene_id"], scene["site"]), []):
            key = f"{hull['scene_id']}__{hull['hull_id']}"
            if key in taken:
                key += f"__{hull['site']}"
            taken.add(key)
            hulls.append(hull | {"id": key, "verdict": hull["run_verdict"]})
    listed, _, _ = report.select_review_hulls(hulls, SINGLES, keep=flags)
    review = {}
    for hull in listed:
        count = review.setdefault(hull["scene_id"], [0, 0, 0])
        count[0] += 1
        count[1] += hull["id"] in flags
        count[2] += hull["final_verdict"] == "unresolved"
    return {"runs": invocations, "last_event": last_event, "scenes": scenes, "review": review}


def scene_rows() -> list[dict]:
    out = []
    with SCENE_LOCK:
        registered = registry.listing()
        existing = {row["run_no"] for row in registered}
        for no in set(SCENE_CACHE) - existing:
            del SCENE_CACHE[no]
        for row in registered:
            no = row["run_no"]
            path = registry.runs_root() / row["folder"] / "state.db"
            if not path.exists():
                SCENE_CACHE.pop(no, None)
                continue
            signature = file_signature(path)
            hit = SCENE_CACHE.get(no)
            if hit is None or hit[0] != signature:
                hit = (signature, read_scene_index(path))
                SCENE_CACHE[no] = hit
            snapshot = hit[1]
            run_status = status_of(snapshot["runs"], snapshot["last_event"])
            for scene in snapshot["scenes"]:
                status = scene_status(scene["status"], run_status)
                listed, reviewed, unresolved = snapshot["review"].get(scene["scene_id"], (0, 0, 0))
                waiting = status in ("running", "stopped") or run_status == "running"
                review = "waiting for run" if waiting else review_of(
                    status, reviewed, listed, unresolved
                )
                out.append({
                    "run": no, "area": scene["site"], "source": scene["source"],
                    "scene_id": scene["scene_id"], "captured": scene["capture_utc"],
                    "created": scene["updated_utc"], "status": status, "review": review,
                })
    return out


def stamp(store: Store) -> tuple:
    return tuple(store.db.execute(
        "select (select count(*) from hulls), (select max(updated_utc) from scenes), (select count(*) from ais_matches), "
        "(select count(*) from ais_candidates), (select count(*) from runs)"
    ).fetchone())


def cached(no: int) -> tuple[dict, dict]:
    path = folder(no)
    lock = LOCKS.setdefault(no, threading.Lock())
    with lock:
        store = Store(path / "state.db", path)
        try:
            key = stamp(store)
        finally:
            store.close()
        hit = CACHE.get(no)
        if hit and hit[0] == key:
            return hit[1], hit[2]
        doc, records = report.collect(path, SINGLES, keep=flagged_ids(no))
        CACHE[no] = (key, doc, records)
        return doc, records


def finals(no: int) -> dict:
    path = folder(no)
    store = Store(path / "state.db", path)
    try:
        return {f"{r['scene_id']}__{r['hull_id']}": r["verdict"] for r in store.db.execute("select scene_id, hull_id, verdict from findings")}
    finally:
        store.close()


def flagged_ids(no: int) -> set:
    return {f["hull_id"] for f in qa_flags(folder(no))}


def detail(no: int) -> dict:
    doc, _ = cached(no)
    final = finals(no)
    hulls = [h | {"final_verdict": final.get(h["id"], h["verdict"])} for h in doc["hulls"]]
    by_id = {h["id"]: h for h in hulls}
    flags = []
    for flag in qa_flags(folder(no)):
        h = by_id.get(flag["hull_id"])
        if h is None:
            flags.append(flag)
            continue
        shown = flag | {"verdict": h["verdict"]}
        if flag["status"] in ("ok", "wrong_verdict", "is_single", "is_moving", "is_pair") and h["final_verdict"] in ("pair", "single"):
            if h["verdict"] == "unmeasured":
                shown |= {"status": f"is_{h['final_verdict']}", "correct_verdict": None}
            elif h["verdict"] == h["final_verdict"]:
                shown |= {"status": "ok", "correct_verdict": None}
            else:
                shown |= {"status": "wrong_verdict", "correct_verdict": h["final_verdict"]}
        flags.append(shown)
    row = next(r for r in registry.listing() if r["run_no"] == no)
    return doc | {"number": no, "summary": summary(row), "hulls": hulls, "flags": flags}


def image(no: int, hull_id: str, kind: str) -> bytes | None:
    _, records = cached(no)
    if hull_id not in records:
        return None
    path = folder(no)
    key = hashlib.sha1(f"{hull_id}|{CACHE[no][0]}".encode()).hexdigest()[:16]
    target = path / "cache" / f"{key}_{kind}.png"
    if target.exists():
        return target.read_bytes()
    scene, rec = records[hull_id]
    img = report.render(path, scene, rec, rules_mod.load(), kind == "overlay")
    if img is None:
        return None
    target.parent.mkdir(exist_ok=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    target.write_bytes(buf.getvalue())
    return buf.getvalue()


def check_flag(h: dict, body: dict) -> str | None:
    status = body.get("status")
    allowed = LABELS if h["verdict"] == "unmeasured" else FLAGS
    if status not in allowed:
        return f"status must be one of {', '.join(allowed)}"
    if status == "wrong_verdict":
        alt = [v for v in CORRECT if v != h["verdict"]]
        if body.get("correct_verdict") not in alt:
            return f"correct_verdict must be one of {', '.join(alt)}"
    return None


def save_flag(no: int, hull_id: str, body: dict) -> tuple[dict | None, str | None]:
    store = Store(folder(no) / "state.db", folder(no))
    try:
        matches = store.db.execute(
            "select h.scene_id, s.source, h.verdict, h.lat, h.lon from hulls h "
            "join scenes s using (scene_id, site) "
            "where (h.scene_id || '__' || h.hull_id = ? "
            "or h.scene_id || '__' || h.hull_id || '__' || h.site = ?) "
            "and coalesce(h.role, '') not in ('duplicate', 'hull_b_box')",
            (hull_id, hull_id),
        ).fetchall()
    finally:
        store.close()
    if len(matches) != 1:
        return None, "unknown hull"
    h = dict(matches[0]) | {"id": hull_id}
    if h["verdict"] == "moving":
        h["verdict"] = "single"
    error = check_flag(h, body)
    if error:
        return None, error
    row = {k: None for k in QA_COLUMNS} | {
        "hull_id": h["id"], "scene_id": h["scene_id"], "source": h["source"], "verdict": h["verdict"], "lat": h["lat"], "lon": h["lon"],
        "status": body["status"], "correct_verdict": body.get("correct_verdict") if body["status"] == "wrong_verdict" else None,
        "note": (body.get("note") or "").strip(), "reviewer": body.get("reviewer") or "local", "updated_utc": now(),
    }
    upsert_flags(folder(no), [row])
    with LOCKS.setdefault(no, threading.Lock()):
        CACHE.pop(no, None)
    mirror(no)
    return row | {"final_verdict": finals(no).get(hull_id)}, None


def mirror(no: int) -> Path:
    flags = qa_flags(folder(no))
    target = reviews_dir() / f"{no}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(flags, indent=1), encoding="utf-8")
    return target
