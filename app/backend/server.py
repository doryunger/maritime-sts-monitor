import hashlib
import json
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.backend import queue, runs, s3runs
from maritime_tip_cue.config import settings

DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

app = FastAPI(title="STS runs")


def known(no: int) -> None:
    try:
        runs.folder(no)
    except KeyError:
        raise HTTPException(404, f"run {no} is not registered")


@app.get("/api/runs")
def list_runs():
    return runs.summaries()


@app.get("/api/scenes")
def list_scenes(if_none_match: str | None = Header(default=None)):
    payload = json.dumps(runs.scene_rows(), ensure_ascii=False, separators=(",", ":")).encode()
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
    return runs.detail(no)


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
    row, error = runs.save_flag(no, hull_id, body)
    if error:
        raise HTTPException(400, error)
    return row


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
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
