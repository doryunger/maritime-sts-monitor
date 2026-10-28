import hashlib
import io
import threading
from datetime import UTC, datetime
from pathlib import Path

from psycopg.rows import dict_row

from maritime_tip_cue import pg
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry, report
from maritime_tip_cue.watch.pgutil import iso
from maritime_tip_cue.watch.state import IGNORED_ROLES, Store

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

STAMPS = (
    "select r.run_key, r.run_no, "
    "(select count(*) from review.scenes s where s.run_key = r.run_key) as scenes, "
    "(select max(s.updated_utc) from review.scenes s where s.run_key = r.run_key) as scenes_at, "
    "(select count(*) from review.hulls h where h.run_key = r.run_key) as hulls, "
    "(select count(*) from review.qa_flags f where f.run_key = r.run_key) as flags, "
    "(select max(f.updated_utc) from review.qa_flags f where f.run_key = r.run_key) as flags_at, "
    "(select count(*) from review.ais_matches m where m.run_key = r.run_key) as matches, "
    "(select count(*) from review.ais_candidates c where c.run_key = r.run_key) as candidates, "
    "(select max(a.checked_utc) from review.ais_coverage a where a.run_key = r.run_key) as coverage_at, "
    "(select count(*) from review.run_invocations i where i.run_key = r.run_key) as invocations, "
    "(select max(i.ended_utc) from review.run_invocations i where i.run_key = r.run_key) as ended_at, "
    "(select max(e.event_id) from review.events e where e.run_key = r.run_key) as events "
    "from review.runs r"
)


def query(sql: str, params=()) -> list[dict]:
    with pg.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def folder(no: int) -> Path:
    return registry.folder_of(no)


def status_of(runs: list[dict], last_event: str | None) -> str:
    if not runs:
        return "empty"
    last = runs[-1]
    if last["ended_utc"]:
        return last["status"] or "done"
    if last_event:
        age = datetime.now(UTC) - datetime.fromisoformat(last_event.replace("Z", "+00:00"))
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


def grouped(rows: list[dict], key: str = "run_key") -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def summaries(only: int | None = None) -> list[dict]:
    where = "" if only is None else " where run_key = %s"
    args = () if only is None else (only,)
    with pg.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        def fetch(sql, params=args):
            cur.execute(sql, params)
            return cur.fetchall()

        runs = fetch(f"select run_key, run_no, created_utc from review.runs{where} order by run_no")
        invocations = grouped(fetch(f"select * from review.run_invocations{where} order by run_key, invocation_id"))
        scene_status = grouped(fetch(f"select run_key, status, count(*) as n from review.scenes{where} group by run_key, status"))
        spans = {r["run_key"]: r for r in fetch(f"select run_key, min(capture_utc) as first, max(capture_utc) as last from review.scenes{where} group by run_key")}
        events = {r["run_key"]: r["at"] for r in fetch(f"select run_key, max(time_utc) as at from review.events{where} group by run_key")}
        run_v = grouped(fetch(f"select run_key, run_verdict as v, count(*) as n from review.findings{where} group by run_key, run_verdict"))
        final_v = grouped(fetch(f"select run_key, verdict as v, count(*) as n from review.findings{where} group by run_key, verdict"))
        reviewed = {r["run_key"]: r["n"] for r in fetch(f"select run_key, count(*) as n from review.qa_flags{where} group by run_key")}
        flagged = {
            r["run_key"]: r["n"]
            for r in fetch(
                "select f.run_key, count(*) as n from review.qa_flags f where not exists (select 1 from review.hulls h "
                "where h.run_key = f.run_key and h.scene_id || '__' || h.hull_id = f.hull_id "
                "and coalesce(h.role, '') not in ('duplicate', 'hull_b_box') "
                "and (case when h.verdict = 'moving' then 'single' else h.verdict end) in ('pair', 'possible_pair', 'unmeasured'))"
                + ("" if only is None else " and f.run_key = %s")
                + " group by f.run_key"
            )
        }
    out = []
    for row in runs:
        key = row["run_key"]
        inv = [r | {"started_utc": iso(r["started_utc"]), "ended_utc": iso(r["ended_utc"])} for r in invocations.get(key, [])]
        named = [r for r in inv if r["hot_spot"]]
        scenes = {r["status"]: r["n"] for r in scene_status.get(key, [])}
        runv = {r["v"]: r["n"] for r in run_v.get(key, [])}
        final = {r["v"]: r["n"] for r in final_v.get(key, [])}
        span = spans.get(key)
        item = {"run": row["run_no"], "created": iso(row["created_utc"]), "area": None, "status": "empty"}
        if not inv and not scenes:
            out.append(item)
            continue
        listed = sum(runv.get(k, 0) for k in LISTED) + max(min(SINGLES, runv.get("single", 0)), flagged.get(key, 0))
        status = status_of(inv, iso(events.get(key)))
        out.append(item | {
            "area": named[-1]["hot_spot"] if named else None,
            "window": [inv[0]["window_start"], inv[-1]["window_end"]] if inv else None,
            "sources": named[-1]["sources"] if named else None,
            "status": status,
            "review": review_of(status, reviewed.get(key, 0), listed, final.get("unresolved", 0)),
            "captured": [iso(span["first"]), iso(span["last"])] if span else [None, None],
            "finished": inv[-1]["ended_utc"] if inv else None,
            "scenes": sum(scenes.values()),
            "scenes_failed": scenes.get("failed", 0),
            "run_verdicts": runv,
            "verdicts": final,
            "reviewed": reviewed.get(key, 0),
            "listed": listed,
        })
    return out


def hotspot_test_pair_counts() -> dict[int, dict[str, int]]:
    found = query(
        "select r.run_no, "
        "count(*) filter (where f.run_verdict = 'pair') as run_pair, "
        "count(*) filter (where f.run_verdict = 'possible_pair') as run_possible_pair, "
        "count(*) filter (where f.verdict = 'pair') as pair, "
        "count(*) filter (where f.verdict = 'possible_pair') as possible_pair "
        "from review.runs r "
        "join review.findings f on f.run_key = r.run_key "
        "join review.hulls h on h.run_key = f.run_key and h.scene_id = f.scene_id and h.site = f.site and h.hull_id = f.hull_id "
        "where f.run_verdict in ('pair', 'possible_pair') and coalesce(h.role, '') not in ('duplicate', 'hull_b_box') "
        "and exists (select 1 from review.run_invocations i where i.run_key = r.run_key and left(i.hot_spot, 5) = 'test_') "
        "group by r.run_no"
    )
    return {
        row["run_no"]: {
            "run_pair": row["run_pair"],
            "run_possible_pair": row["run_possible_pair"],
            "pair": row["pair"],
            "possible_pair": row["possible_pair"],
        }
        for row in found
    }


def summary(row: dict) -> dict:
    found = summaries(row["run_key"])
    return found[0] if found else {"run": row["run_no"], "created": row["created_utc"], "area": None, "status": "empty"}


def scene_status(status: str, run_status: str) -> str:
    if status == "checked":
        return "done"
    if status == "failed":
        return "failed"
    return "running" if run_status == "running" else "stopped"


def stamps() -> dict[int, tuple]:
    return {r["run_no"]: tuple(v for k, v in r.items() if k not in ("run_no",)) for r in query(STAMPS)}


def read_scene_index(run_key: int) -> dict:
    with pg.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        def fetch(sql):
            cur.execute(sql, (run_key,))
            return cur.fetchall()

        invocations = [{"ended_utc": iso(r["ended_utc"]), "status": r["status"]} for r in fetch(
            "select ended_utc, status from review.run_invocations where run_key = %s order by invocation_id"
        )]
        last_event = fetch("select max(time_utc) as at from review.events where run_key = %s")[0]["at"]
        scenes = [r | {"capture_utc": iso(r["capture_utc"]), "updated_utc": iso(r["updated_utc"])} for r in fetch(
            "select scene_id, site, source, capture_utc, status, updated_utc from review.scenes where run_key = %s "
            "order by capture_utc, scene_id, site"
        )]
        flags = {r["hull_id"] for r in fetch("select hull_id from review.qa_flags where run_key = %s")}
        found = fetch(
            "select scene_id, site, hull_id, role, run_verdict, verdict as final_verdict from review.findings "
            "where run_key = %s order by hull_id"
        )
    groups: dict = {}
    for hull in found:
        if hull["role"] in IGNORED_ROLES:
            continue
        groups.setdefault((hull["scene_id"], hull["site"]), []).append(hull)
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
    return {"runs": invocations, "last_event": iso(last_event), "scenes": scenes, "review": review}


def scene_rows() -> list[dict]:
    out = []
    with SCENE_LOCK:
        registered = registry.listing()
        current = stamps()
        for no in set(SCENE_CACHE) - {row["run_no"] for row in registered}:
            del SCENE_CACHE[no]
        for row in registered:
            no = row["run_no"]
            signature = current.get(no)
            hit = SCENE_CACHE.get(no)
            if hit is None or hit[0] != signature:
                hit = (signature, read_scene_index(row["run_key"]))
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
    row = store.db.execute(STAMPS + " where r.run_key = %s", (store.run_key,)).fetchone()
    return tuple(v for k, v in row.items() if k != "run_no")


def cached(no: int) -> tuple[dict, dict, tuple]:
    lock = LOCKS.setdefault(no, threading.Lock())
    with lock:
        store = Store.open(no)
        try:
            key = stamp(store)
            hit = CACHE.get(no)
            if hit and hit[0] == key:
                return hit[1], hit[2], key
            doc, records = report.collect(store, SINGLES, keep={f["hull_id"] for f in store.qa_flags()})
        finally:
            store.close()
        CACHE[no] = (key, doc, records)
        return doc, records, key


def detail(no: int) -> dict:
    doc, _, key = cached(no)
    store = Store.open(no)
    try:
        final = store.final_verdicts()
        stored = store.qa_flags()
        row = registry.get(no)
    finally:
        store.close()
    hulls = [h | {"final_verdict": final.get(h["id"], h["verdict"])} for h in doc["hulls"]]
    by_id = {h["id"]: h for h in hulls}
    flags = []
    for flag in stored:
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
    version = hashlib.sha1(repr(key).encode()).hexdigest()[:12]
    return doc | {"number": no, "summary": summary(row), "hulls": hulls, "flags": flags, "image_version": version}


def image(no: int, hull_id: str, kind: str) -> bytes | None:
    _, records, stamp_key = cached(no)
    if hull_id not in records:
        return None
    path = folder(no)
    key = hashlib.sha1(f"{hull_id}|{stamp_key}".encode()).hexdigest()[:16]
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
    store = Store.open(no)
    try:
        target = store.flag_target(hull_id)
        if target is None:
            return None, "unknown hull"
        h = {"verdict": "single" if target["verdict"] == "moving" else target["verdict"]}
        error = check_flag(h, body)
        if error:
            return None, error
        fields = {
            "status": body["status"],
            "correct_verdict": body.get("correct_verdict") if body["status"] == "wrong_verdict" else None,
            "note": (body.get("note") or "").strip(),
            "reviewer": body.get("reviewer") or "local",
        }
        row = store.save_flag(hull_id, fields)
        if row is None:
            return None, "unknown hull"
        final = store.final_verdicts().get(hull_id)
    finally:
        store.close()
    return row | {"final_verdict": final}, None
