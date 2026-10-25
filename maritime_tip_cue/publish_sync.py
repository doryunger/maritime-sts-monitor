import argparse
import json
import sys
from collections import Counter, defaultdict

import pyarrow.parquet as pq

from maritime_tip_cue import publish, publish_merge, task_queue
from maritime_tip_cue.config import env, settings
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch import registry
from maritime_tip_cue.watch.state import Store, now

REJECTED = "rejected"
PROVENANCE = ("attempt_id", "review_prefix", "rules_sha256", "code_commit", "detector_revision")
ACTIONS = ("added", "rejected", "restored", "status_updated")


def quiet(*_args, **_kwargs) -> None:
    return None


def desired(finding: dict) -> tuple[str, str]:
    if finding["verdict"] == "pair":
        return publish.QA_LABEL.get(finding["qa_status"], "unreviewed"), "pair"
    return REJECTED, finding["verdict"] or "unresolved"


def classify(row: dict, want: tuple[str, str]) -> str | None:
    qa, verdict = want
    if (row.get("qa_status"), row.get("verdict")) == (qa, verdict):
        return None
    if qa == REJECTED:
        return "rejected"
    return "restored" if row.get("qa_status") == REJECTED else "status_updated"


def choose_runs(requested: list[int]) -> list[dict]:
    listing = registry.listing()
    if requested:
        by_no = {r["run_no"]: r for r in listing}
        missing = [no for no in requested if no not in by_no]
        if missing:
            raise SystemExit(f"no such run: {missing}")
        return [by_no[no] for no in requested]
    keys = {
        r["run_key"]
        for r in registry.fetch(None, "select run_key from review.outputs union select run_key from review.findings where verdict = 'pair'")
    }
    return [r for r in listing if r["run_key"] in keys]


def gate(runs: list[dict], explicit: bool) -> tuple[list[dict], dict]:
    completed = task_queue.client().smembers(task_queue.keys()["completed"])
    problems = {r["run_no"]: reason for r in runs if (reason := publish.run_problem(r["run_no"], completed, r["origin"] == "legacy"))}
    if problems and explicit:
        detail = "; ".join(f"run {no}: {reason}" for no, reason in sorted(problems.items()))
        raise SystemExit(f"Refusing to sync, nothing written: {detail}")
    return [r for r in runs if r["run_no"] not in problems], problems


def read_published(client, bucket: str, prefix: str) -> dict[str, list[dict]]:
    return {
        key: publish_merge.read_rows(client, bucket, key)[0]
        for key in publish_merge.list_keys(client, bucket, prefix)
        if key.endswith(".parquet")
    }


def finding_length(finding: dict) -> float | None:
    rec = finding["record"]
    return rec.get("length_m") or (rec.get("check") or {}).get("hull_a_length_m") or finding["ai2_length_m"]


def pick_owner(row: dict, held: list[dict]) -> int | None:
    if row.get("attempt_id"):
        named = [h for h in held if h["attempt_id"] == row["attempt_id"]]
        if len(named) == 1:
            return named[0]["run_key"]
    if row.get("job_id"):
        named = [h for h in held if h["job_id"] == row["job_id"]]
        if len(named) == 1:
            return named[0]["run_key"]
    else:
        named = [h for h in held if h["folder"] == f"legacy-run-{row.get('run')}"]
        if len(named) == 1:
            return named[0]["run_key"]
    if len(held) == 1:
        return held[0]["run_key"]
    return None


def resolve_owners(files: dict) -> tuple[dict, list[dict]]:
    rows = [r for group in files.values() for r in group]
    idents = sorted({(r["scene_id"], r["hot_spot"], r["hull_id"]) for r in rows})
    held: dict = defaultdict(list)
    if idents:
        found = registry.fetch(
            None,
            "select h.scene_id, h.site, h.hull_id, r.run_key, r.folder, r.attempt_id, a.job_id from review.hulls h "
            "join review.runs r on r.run_key = h.run_key left join processing.job_attempts a on a.attempt_id = r.attempt_id "
            "where (h.scene_id, h.site, h.hull_id) in (select * from unnest(%s::text[], %s::text[], %s::text[]))",
            ([i[0] for i in idents], [i[1] for i in idents], [i[2] for i in idents]),
        )
        for f in found:
            held[(f["scene_id"], f["site"], f["hull_id"])].append(f)
    owners, unmatched = {}, []
    for row in rows:
        owner = pick_owner(row, held.get((row["scene_id"], row["hot_spot"], row["hull_id"]), []))
        if owner is None:
            unmatched.append(row)
        else:
            owners[row["finding_id"]] = owner
    return owners, unmatched


def plan_run(run: dict, files: dict, rule: dict, prefix: str, owners: dict) -> dict:
    store = Store.open(run["run_no"])
    try:
        findings = store.db.execute(
            "select f.scene_id, f.site, f.hull_id, f.verdict, f.qa_status, f.source, f.capture_utc, f.lat, f.lon, h.record, h.ai2_length_m "
            "from review.findings f join review.hulls h on h.run_key = f.run_key and h.scene_id = f.scene_id and h.site = f.site "
            "and h.hull_id = f.hull_id where f.run_key = %s",
            (store.run_key,),
        ).fetchall()
    finally:
        store.close()
    index = {(f["scene_id"], f["site"], f["hull_id"]): f for f in findings}
    out = {"edits": defaultdict(dict), "counts": defaultdict(Counter), "adds": {}, "duplicates": 0, "published": set(), "rows": {}}
    for key, rows in files.items():
        for row in rows:
            if owners.get(row["finding_id"]) != run["run_key"]:
                continue
            ident = (row["scene_id"], row["hot_spot"], row["hull_id"])
            out["published"].add(ident)
            out["rows"].setdefault(key, []).append(row)
            counts = out["counts"][key]
            finding = index.get(ident)
            action = classify(row, desired(finding)) if finding else None
            if action is None:
                counts["unchanged"] += 1
                continue
            counts[action] += 1
            qa, verdict = desired(finding)
            out["edits"][key][row["finding_id"]] = {"qa_status": qa, "verdict": verdict}
    for ident, finding in sorted(index.items()):
        if finding["verdict"] != "pair" or ident in out["published"]:
            continue
        candidate = {
            "source": finding["source"], "capture_utc": finding["capture_utc"], "lat": finding["lat"], "lon": finding["lon"],
            "measures": json.dumps({"length_m": finding_length(finding)}),
        }
        if any(publish_merge.twin(candidate, e, rule) for rows in files.values() for e in rows):
            out["duplicates"] += 1
            continue
        key = f"{prefix}{publish.ddmmyyyy(publish.iso(finding['capture_utc']))}/{finding['site']}/{finding['source']}.parquet"
        out["adds"][ident] = key
        out["counts"][key]["added"] += 1
    return out


def provenance_for(run: dict, rows: list[dict]) -> dict:
    if rows:
        return {k: rows[0].get(k) for k in PROVENANCE}
    return {"attempt_id": run["attempt_id"]}


def build_additions(run: dict, plan: dict, rules: dict) -> list[dict]:
    from maritime_tip_cue.ais import match

    wanted = set(plan["adds"])
    store = Store.open(run["run_no"])
    try:
        rows = [r for r in match.select_hulls(store, match.PAIR_VERDICTS, False, False) if (r["scene_id"], r["site"], r["hull_id"]) in wanted]
        match.run_match(store, rows, rules, quiet)
    finally:
        store.close()
    found = publish.complete(publish.collect_at(registry.folder_of(run["run_no"]), run["run_no"], None, rules, wanted))
    provenance = provenance_for(run, [row for rows in plan["rows"].values() for row in rows])
    for f in found:
        f["provenance"] = provenance
    return found


def upload_changes(client, bucket: str, tables: dict, edits: dict, rules: dict) -> dict:
    prefix_f, prefix_c = settings()["s3"]["findings_prefix"], settings()["s3"]["captures_prefix"]
    root = publish.out_root()
    by_key = {prefix_f + path.relative_to(root / "sts").as_posix(): (path, items) for path, items in tables.values()}
    outcomes = {}
    for key in sorted(set(by_key) | set(edits)):
        path, items = by_key.get(key, (None, []))
        by_id = {f["finding_id"]: f for f in items}

        def push(kept_rows, by_id=by_id):
            for row in kept_rows:
                for p in by_id[row["finding_id"]]["files"].values():
                    client.upload_file(str(p), bucket, prefix_c + p.relative_to(root / "captures").as_posix(), ExtraArgs={"ContentType": "image/png"})

        date = key[len(prefix_f):].split("/")[0]
        new_rows = pq.read_table(path).to_pylist() if path else []
        outcomes[key] = publish_merge.merge_group(client, bucket, prefix_f, date, key, new_rows, None, rules["publish"], push, edits.get(key))
    return outcomes


def record(bucket: str, outcomes: dict, tables: dict, plans: dict) -> None:
    prefix_f, prefix_c = settings()["s3"]["findings_prefix"], settings()["s3"]["captures_prefix"]
    root = publish.out_root()
    per_run: dict[int, list] = defaultdict(list)
    for path, items in tables.values():
        key = prefix_f + path.relative_to(root / "sts").as_posix()
        kept = set(outcomes[key]["kept"])
        for no in sorted({f["run"] for f in items}):
            mine = [f for f in items if f["run"] == no and f["finding_id"] in kept]
            if not mine:
                continue
            for f in mine:
                for fmt, p in f["files"].items():
                    ckey = prefix_c + p.relative_to(root / "captures").as_posix()
                    per_run[no].append(("capture", f["finding_id"], fmt, str(p), f"s3://{bucket}/{ckey}", f["scene_id"], f["site"], f["hull_id"]))
            existing = plans[no]["rows"].get(key, [])
            scenes = sorted({f["scene_id"] for f in mine} | {row["scene_id"] for row in existing})
            total = len(existing) + len(mine)
            date = key[len(prefix_f):].split("/")[0]
            per_run[no].append(("output", str(path), f"s3://{bucket}/{key}", date, mine[0]["site"], mine[0]["source"], total, total, scenes, outcomes[key]["sha256"]))
    publish.record_entries(per_run, now(), True)


def sync(client, bucket: str, requested: list[int], upload: bool, dry_run: bool, rules: dict | None = None) -> list[str]:
    rules = rules or rules_mod.load()
    prefix = settings()["s3"]["findings_prefix"]
    runs, skipped = gate(choose_runs(requested), bool(requested))
    lines = [f"run {no} skipped: {reason}" for no, reason in sorted(skipped.items())]
    files = read_published(client, bucket, prefix)
    owners, unmatched = resolve_owners(files)
    if unmatched:
        lines.append(f"{len(unmatched)} published rows could not be attributed to one run and were left as they are")
    plans = {r["run_no"]: plan_run(r, files, rules["publish"], prefix, owners) for r in runs}
    totals: dict[str, Counter] = defaultdict(Counter)
    edits: dict[str, dict] = defaultdict(dict)
    for plan in plans.values():
        for key, counts in plan["counts"].items():
            totals[key].update(counts)
        for key, changes in plan["edits"].items():
            edits[key].update(changes)
    duplicates = sum(p["duplicates"] for p in plans.values())
    tables: dict = {}
    if not dry_run and any(p["adds"] for p in plans.values()):
        found = []
        for run in runs:
            if plans[run["run_no"]]["adds"]:
                found += build_additions(run, plans[run["run_no"]], rules)
        _, tables = publish.assemble(found, rules, publish.out_root())
    if upload and not dry_run and (edits or tables):
        outcomes = upload_changes(client, bucket, tables, edits, rules)
        for key, outcome in outcomes.items():
            totals[key]["added"] = len(outcome["kept"])
            duplicates += len(outcome["skipped"])
        record(bucket, outcomes, tables, plans)
    changed = 0
    for key in sorted(totals):
        counts = totals[key]
        if not any(counts[a] for a in ACTIONS):
            continue
        changed += 1
        lines.append(f"{key}: " + ", ".join(f"{a} {counts[a]}" for a in (*ACTIONS, "unchanged")))
    rows = sum(len(r) for p in plans.values() for r in p["rows"].values())
    if changed:
        mode = "dry run, nothing written" if dry_run else ("uploaded" if upload else f"added rows written locally to {publish.out_root()}; edits need --upload")
        lines.append(f"{changed} files change ({mode}); {duplicates} pairs skipped as duplicates")
    else:
        lines.append(f"nothing to change: {len(plans)} runs, {rows} published rows checked, {duplicates} pairs skipped as duplicates")
    return lines


def main(argv: list[str]) -> int:
    import boto3

    parser = argparse.ArgumentParser(prog="maritime-sts-monitor publish sync")
    parser.add_argument("--runs", nargs="+", type=int, default=[])
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    for line in sync(client, env("S3_BUCKET_NAME"), args.runs, args.upload, args.dry_run):
        print(line)
    return 0
