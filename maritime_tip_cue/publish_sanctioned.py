import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point

from maritime_tip_cue import publish, sanctions
from maritime_tip_cue.config import env, settings
from maritime_tip_cue.publish_merge import GEO
from maritime_tip_cue.sts import rules as rules_mod

FOLDER = "sanctioned"
PREFIX_KEY = "sanctioned_prefix"


def sanctioned_party(f: dict) -> dict | None:
    return next((p for _, p in sorted(f["parties"].items()) if p.get("is_sanctioned")), None)


def assemble(findings: list[dict], rules: dict, root: Path, provenance: dict | None = None) -> tuple[list[dict], dict]:
    kept = publish.dedupe(findings, rules["publish"])
    groups: dict[tuple, list] = defaultdict(list)
    for f in kept:
        stamp = publish.capture_time(f).strftime("%Y%m%dT%H%M%S")
        f["finding_id"] = f"{f['source']}_{stamp}_{publish.safe(f['hull_id'])}"
        f["party"] = sanctioned_party(f)
        base = "_".join([f["finding_id"]] + ([publish.safe(f["party"]["name"])] if f["party"].get("name") else []))
        f["files"] = publish.write_captures(f, base, rules, root, single=True)
        groups[(publish.ddmmyyyy(f["capture_utc"]), f["site"], f["source"])].append(f)
    prefix_c = settings()["s3"]["captures_prefix"]
    tables = {}
    for (date, site, source), items in sorted(groups.items()):
        rows = []
        for f in sorted(items, key=lambda f: (f["capture_utc"], f["hull_id"])):
            pos = publish.vessel_positions(f["source"], f["record"], f["lat"], f["lon"])[0] or (f["lat"], f["lon"])
            rel = {k: p.relative_to(root / "captures").as_posix() for k, p in f["files"].items()}
            heading, heading_source = publish.pair_heading(f)
            rows.append(
                {
                    "finding_id": f["finding_id"],
                    "geometry": Point(f["lon"], f["lat"]).wkb,
                    "lat": f["lat"], "lon": f["lon"],
                    "capture_utc": publish.capture_time(f),
                    "capture_date": date,
                    "hot_spot": site,
                    "also_in": ";".join(f["also"]) or None,
                    "source": source,
                    "scene_id": f["scene_id"],
                    "hull_id": f["hull_id"],
                    "run": f["run"],
                    "job_id": f.get("job_id"),
                    **(provenance or {}),
                    **f.get("provenance", {}),
                    "verdict": f["verdict"],
                    "run_verdict": f["run_verdict"],
                    "qa_status": f["qa"],
                    "qa_note": f["qa_note"] or None,
                    "measures": json.dumps(publish.measures(f)),
                    "heading_deg": heading,
                    "heading_source": heading_source,
                    **{f"vessel_{key}": value for key, value in publish.vessel_fields(f["party"], pos).items()},
                    "vessel_sanctions_programs": f["party"].get("sanctions_programs"),
                    "vessel_match_score": f["party"].get("match_score"),
                    "vessel_ais_heading_deg": f["party"].get("ais_heading_deg"),
                    "vessel_ais_speed_kn": f["party"].get("ais_speed_kn"),
                    "ais_covered": f["ais_covered"],
                    "ais_coverage_ships": f["ais_coverage_ships"],
                    "capture_png": prefix_c + rel["png"] if "png" in rel else None,
                }
            )
        table = pa.Table.from_pylist(rows)
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), b"geo": json.dumps(GEO).encode()})
        path = root / FOLDER / date / site / f"{source}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path)
        tables[(date, site, source)] = (path, items)
    return kept, tables


def publish_findings(client, bucket: str, findings: list[dict], rules: dict, root: Path, job_id: str | None, provenance: dict | None = None) -> dict:
    kept, tables = assemble(findings, rules, root, provenance)
    outcomes = publish.upload_tables(client, bucket, tables, rules, root, job_id, FOLDER, PREFIX_KEY) if tables else {}
    return {
        "listed_imos": len(sanctions.table()),
        "findings": len(kept),
        "published": sum(len(o["kept"]) for o in outcomes.values()),
        "duplicates_skipped": sum(len(o["skipped"]) for o in outcomes.values()),
        "files": sorted(o["key"] for o in outcomes.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime-sts-monitor publish_sanctioned")
    parser.add_argument("runs", nargs="+", type=int)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--match", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rules = rules_mod.load()
    verdicts = tuple(rules["sanctioned"]["verdicts"])
    if args.match:
        from maritime_tip_cue.ais import match
        from maritime_tip_cue.watch.state import Store

        for no in args.runs:
            store = Store.open(no)
            try:
                match.match_singles(store, rules)
            finally:
                store.close()
    found = []
    for no in args.runs:
        found += publish.complete(publish.collect_at(publish.registry.folder_of(no), no, None, None, verdicts=verdicts))
    if args.upload:
        import boto3

        client = boto3.client("s3", region_name=env("AWS_REGION"))
        summary = publish_findings(client, env("S3_BUCKET_NAME"), found, rules, Path(tempfile.mkdtemp(prefix="sanctioned-")), None)
    else:
        kept, tables = assemble(found, rules, publish.out_root())
        summary = {"findings": len(kept), "files": sorted(str(path) for path, _ in tables.values())}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
