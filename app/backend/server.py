import hashlib
import json
import os
import sys
from pathlib import Path

import boto3
import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.backend import auth, queue, runs, s3runs, sync
from maritime_tip_cue import hotspot_tests
from maritime_tip_cue.config import env, settings

DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

app = FastAPI(title="STS runs")
auth.install(app)


def known(no: int) -> None:
    try:
        runs.folder(no)
    except KeyError:
        raise HTTPException(404, f"run {no} is not registered")


@app.get("/api/runs")
def list_runs():
    return [row for row in runs.summaries() if not (row.get("area") or "").startswith("test_")]


@app.get("/api/scenes")
def list_scenes(if_none_match: str | None = Header(default=None)):
    rows = [row for row in runs.scene_rows() if not row["area"].startswith("test_")]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
    if etag in tags or "*" in tags:
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/json", headers=headers)


@app.get("/api/jobs")
def list_jobs():
    return queue.snapshot()


@app.get("/api/runs/{no}")
def get_run(no: int):
    known(no)
    doc = runs.detail(no)
    if doc["run"]["hot_spot"].startswith("test_"):
        raise HTTPException(404, "Use the hotspot-test review page")
    return doc


@app.get("/api/hotspot-tests/areas")
def list_hotspot_test_areas():
    return hotspot_tests.load_plan()["areas"]


@app.get("/api/hotspot-tests/runs")
def list_hotspot_test_runs():
    output = []
    pair_counts = runs.hotspot_test_pair_counts()
    for row in runs.summaries():
        if not (row.get("area") or "").startswith("test_"):
            continue
        counts = pair_counts.get(row["run"], {})
        run_verdicts = {
            "pair": counts.get("run_pair", 0),
            "possible_pair": counts.get("run_possible_pair", 0),
        }
        verdicts = {key: counts.get(key, 0) for key in ("pair", "possible_pair")}
        output.append(
            row
            | {
                "run_verdicts": run_verdicts,
                "verdicts": verdicts,
                "listed": sum(run_verdicts.values()),
            }
        )
    return output


@app.get("/api/hotspot-tests/runs/{no}")
def get_hotspot_test_run(no: int):
    known(no)
    doc = runs.detail(no)
    if not doc["run"]["hot_spot"].startswith("test_"):
        raise HTTPException(404, "Run is not a hotspot-test run")
    hulls = [
        hull
        for hull in doc["hulls"]
        if hull["verdict"] in ("pair", "possible_pair") and hull.get("role") not in ("duplicate", "hull_b_box")
    ]
    ids = {hull["id"] for hull in hulls}
    summary = doc["summary"]
    counts = runs.hotspot_test_pair_counts().get(no, {})
    pair_counts = {"pair": counts.get("run_pair", 0), "possible_pair": counts.get("run_possible_pair", 0)}
    final_counts = {key: counts.get(key, 0) for key in ("pair", "possible_pair")}
    scene_ids = {hull["scene_id"] for hull in hulls}
    scenes = [
        {key: value for key, value in scene.items() if key not in ("raw", "kept", "checked", "single", "unmeasured")}
        for scene in doc["scenes"]
        if scene["scene_id"] in scene_ids
    ]
    test_summary = summary | {"run_verdicts": pair_counts, "verdicts": final_counts, "listed": len(hulls)}
    return doc | {
        "hulls": hulls,
        "flags": [flag for flag in doc["flags"] if flag["hull_id"] in ids],
        "scenes": scenes,
        "summary": test_summary,
    }


@app.put("/api/hotspot-tests/runs/{no}/flags/{hull_id}")
def put_hotspot_test_flag(no: int, hull_id: str, body: dict = Body(...)):
    known(no)
    doc = runs.detail(no)
    if not doc["run"]["hot_spot"].startswith("test_"):
        raise HTTPException(404, "Run is not a hotspot-test run")
    candidate = next(
        (
            hull
            for hull in doc["hulls"]
            if hull["id"] == hull_id
            and hull["verdict"] in ("pair", "possible_pair")
            and hull.get("role") not in ("duplicate", "hull_b_box")
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(404, "Unknown hotspot-test pair candidate")
    row, error = runs.save_flag(no, hull_id, body)
    if error:
        raise HTTPException(400, error)
    return row


@app.get("/api/hotspot-tests/parquets")
def list_hotspot_test_parquets():
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    prefix = hotspot_tests.findings_prefix()
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=env("S3_BUCKET_NAME"), Prefix=prefix)
    return [
        {"key": item["Key"], "size": item["Size"], "last_modified": item["LastModified"].isoformat()}
        for page in pages
        for item in page.get("Contents", [])
        if item["Key"].endswith(".parquet")
    ]


@app.get("/api/hotspot-tests/parquets/{key:path}")
def get_hotspot_test_parquet(key: str):
    prefix = hotspot_tests.findings_prefix()
    if not key.startswith(prefix) or ".." in key.split("/"):
        raise HTTPException(404)
    client = boto3.client("s3", region_name=env("AWS_REGION"))
    try:
        response = client.get_object(Bucket=env("S3_BUCKET_NAME"), Key=key)
    except Exception as error:
        raise HTTPException(404, "Hotspot-test Parquet was not found") from error
    name = Path(key).name
    return Response(
        response["Body"].read(),
        media_type="application/vnd.apache.parquet",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"},
    )


@app.get("/api/runs/{no}/hulls/{hull_id}/{kind}.png")
def get_image(no: int, hull_id: str, kind: str):
    known(no)
    if kind not in ("raw", "overlay"):
        raise HTTPException(404)
    data = runs.image(no, hull_id, kind)
    if data is None:
        raise HTTPException(404, "no pixels stored for this hull")
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.put("/api/runs/{no}/flags/{hull_id}")
def put_flag(no: int, hull_id: str, body: dict = Body(...)):
    known(no)
    doc = runs.detail(no)
    if doc["run"]["hot_spot"].startswith("test_"):
        raise HTTPException(404, "Use the hotspot-test review page")
    row, error = runs.save_flag(no, hull_id, body)
    if error:
        raise HTTPException(400, error)
    return row


@app.get("/api/sync")
def get_sync():
    return sync.current()


@app.post("/api/sync")
def post_sync(request: Request):
    created, state = sync.request(getattr(request.state, "user", None) or "local")
    return JSONResponse(state, status_code=202 if created else 409)


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        return FileResponse(DIST / "index.html")


def main() -> int:
    cfg = settings()["app"]
    if os.environ.get("STS_RUNS_FROM_S3") == "1":
        s3runs.start()
    if sys.stdout is None:
        log = open(Path(__file__).resolve().parent.parent / "server.log", "a", encoding="utf-8")
        sys.stdout = sys.stderr = log
    if not DIST.exists():
        print("frontend not built: cd app/frontend && npm install && npm run build", flush=True)
    host = os.environ.get("STS_APP_HOST") or cfg["host"]
    auth.check_exposure(host)
    uvicorn.run(app, host=host, port=int(os.environ.get("STS_APP_PORT") or cfg["port"]), log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
