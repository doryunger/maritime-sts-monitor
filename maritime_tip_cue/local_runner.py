import argparse
import json
import sys
import traceback
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import boto3

from maritime_tip_cue import hotspots, task_queue, worker
from maritime_tip_cue.config import ROOT, env

LEGACY_MESSAGE = (
    "Legacy batch still running or awaiting adoption; "
    "finish/adopt it through the shared queue before starting new work."
)


def session_root() -> Path:
    return ROOT / "data" / "local_runner"


def now() -> str:
    return datetime.now(UTC).isoformat()


def create_queue_session(jobs: list[dict]) -> dict:
    if not jobs:
        raise ValueError("A queue session needs at least one job")
    entries = []
    seen = set()
    for job in jobs:
        if job["job_id"] in seen:
            raise ValueError("Duplicate queue job")
        seen.add(job["job_id"])
        entries.append(
            {
                "job_key": job["job_key"],
                "job_id": job["job_id"],
                "site": job["site"],
                "status": "pending",
                "run_no": None,
                "error": None,
                "resume": job.get("status") in ("claimed", "failed") and bool(job.get("owned")),
            }
        )
    return {"schema_version": 2, "created_utc": now(), "status": "pending", "entries": entries}


def save_session(session: dict, root: Path | None = None) -> None:
    root = root or session_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "session.json.tmp"
    temporary.write_text(json.dumps(session, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(root / "session.json")


def load_session(root: Path) -> dict | None:
    path = root / "session.json"
    if not path.exists():
        return None
    session = json.loads(path.read_text(encoding="utf-8"))
    if session.get("schema_version") not in (1, 2) or not session.get("entries"):
        raise ValueError("Unsupported or empty local session")
    if session["schema_version"] == 2:
        ids = set()
        for entry in session["entries"]:
            for field in ("job_key", "job_id", "site"):
                if not isinstance(entry.get(field), str) or not entry[field]:
                    raise ValueError(f"Invalid queue entry {field}")
            if entry["job_id"] in ids:
                raise ValueError("Duplicate queue job")
            ids.add(entry["job_id"])
    return session


class Tee:
    def __init__(self, terminal, log):
        self.terminal = terminal
        self.log = log

    def write(self, value):
        self.terminal.write(value)
        self.log.write(value)
        self.flush()
        return len(value)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def isatty(self):
        return False


def owns_claim(client, bucket: str, job_id: str) -> bool:
    return task_queue.client().sismember(task_queue.keys()["inflight"], job_id) == 1


def queue_snapshot(client, bucket: str) -> list[dict]:
    rows = worker.list_jobs(client, bucket)
    for row in rows:
        row["owned"] = owns_claim(client, bucket, row["job_id"]) if row["status"] in ("claimed", "failed") else False
    return rows


def print_queue(rows: list[dict], sites: list[dict]) -> None:
    names = list(dict.fromkeys([site["name"] for site in sites] + [row["site"] for row in rows]))
    for name in names:
        selected = [row for row in rows if row["site"] == name]
        counts = {
            status: sum(row["status"] == status for row in selected)
            for status in ("ready", "claimed", "failed", "succeeded")
        }
        print(f"{name}: " + ", ".join(f"{status}={count}" for status, count in counts.items()))
    for row in rows:
        if row["status"] == "claimed":
            recovery = (
                "owned here; select to resume" if row.get("owned") else "unavailable; foreign/missing local owner"
            )
            print(f"  {row['site']} {row['job_id'][:12]}: {row['status']}, {recovery}")


def local_run_no(job_id: str):
    pointer = worker.worker_root() / job_id / "run.json"
    return json.loads(pointer.read_text(encoding="utf-8"))["run_no"] if pointer.exists() else None


def execute_session(session: dict, client, bucket: str, root: Path | None = None, resume: bool = False) -> int:
    if session.get("schema_version") != 2:
        raise RuntimeError(LEGACY_MESSAGE)
    root = root or session_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "session.log").open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
            session["status"] = "running"
            save_session(session, root)
            try:
                for entry in session["entries"]:
                    if entry["status"] == "done":
                        print(f"{entry['site']} {entry['job_id'][:12]}: already completed; skipped", flush=True)
                        continue
                    owner = None
                    manifest = None
                    receipt = None
                    claimed = False
                    try:
                        rows = worker.list_jobs(client, bucket)
                        current = next((row for row in rows if row["job_id"] == entry["job_id"]), None)
                        if (
                            current is None
                            or current["job_key"] != entry["job_key"]
                            or current["site"] != entry["site"]
                        ):
                            raise RuntimeError("Selected job no longer matches the shared queue")
                        manifest = json.loads(task_queue.client().get(task_queue.keys()["manifest"] + entry["job_id"]))
                        if manifest is None:
                            raise FileNotFoundError("Queued job manifest is missing")
                        worker.validate(manifest)
                        if manifest["job_id"] != entry["job_id"] or manifest["site"]["name"] != entry["site"]:
                            raise ValueError("Queued manifest differs from the selected job identity")
                        recovering = resume or bool(entry.get("resume"))
                        owner = f"{worker.worker_root()}:{uuid.uuid4().hex}"
                        claimed = task_queue.claim(entry["job_id"], owner)
                        if claimed is None:
                            raise RuntimeError("Job unavailable; another worker holds its Redis claim")
                        manifest = claimed
                        claimed = True
                        entry.update(status="running", error=None, started_utc=now())
                        save_session(session, root)
                        print(
                            f"{now()} attempt {entry['job_id']} manifest={entry['job_key']} resume={recovering}",
                            flush=True,
                        )
                        receipt = worker.run_job(client, bucket, manifest, resume=recovering, queue_owner=owner)
                        entry.update(
                            status="done" if receipt["status"] == "succeeded" else "failed",
                            run_no=receipt.get("run_no"),
                            error=receipt.get("error"),
                            finished_utc=now(),
                            result_key=(
                                task_queue.keys()["result"]
                                + entry["job_id"]
                                + ":"
                                + manifest["_queue_attempt_id"]
                            ),
                        )
                        save_session(session, root)
                        print(
                            f"{entry['site']}: {entry['status']}, run={entry['run_no']}, result={entry['result_key']}, "
                            f"artifacts={[artifact['key'] for artifact in receipt.get('artifacts', [])]}",
                            flush=True,
                        )
                        if entry["error"]:
                            print(f"  {entry['error']}", flush=True)
                    except KeyboardInterrupt:
                        if claimed:
                            if receipt is None:
                                task_queue.fail_with_result(
                                    entry["job_id"], owner, manifest, "Interrupted from terminal"
                                )
                        entry.update(status="interrupted", error="Interrupted from terminal", finished_utc=now())
                        entry["run_no"] = local_run_no(entry["job_id"])
                        raise
                    except Exception as exc:
                        if claimed:
                            if receipt is None:
                                task_queue.fail_with_result(entry["job_id"], owner, manifest, exc)
                        entry.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_utc=now())
                        entry["run_no"] = local_run_no(entry["job_id"])
                        save_session(session, root)
                        traceback.print_exc()
                        print(f"{entry['site']}: failed; retained for explicit resume. Continuing.", flush=True)
            except KeyboardInterrupt:
                session.update(status="interrupted", finished_utc=now())
                save_session(session, root)
                print("Interrupted. Run with --resume to continue the saved queue session.", flush=True)
                return 130
            failed = any(entry["status"] != "done" for entry in session["entries"])
            session.update(status="failed" if failed else "done", finished_utc=now())
            save_session(session, root)
            print(f"Session {session['status']}. Log: {root / 'session.log'}", flush=True)
            return int(failed)


def ask_selection(available):
    print(f"{len(available)} shared jobs available:")
    for number, job in enumerate(available, 1):
        action = "start" if job["status"] == "ready" else "resume on this machine"
        print(f"  {number}. {job['site']}: {job['scenes']} scenes, {action}, job {job['job_id'][:12]}")
    while True:
        value = input("Choose job numbers (for example 1,3), 'all', or 'q' to quit: ").strip().lower()
        if value == "q":
            return []
        if value == "all":
            return available
        try:
            choices = list(dict.fromkeys(int(part.strip()) for part in value.split(",")))
            if not choices or any(number < 1 or number > len(available) for number in choices):
                raise ValueError
            return [available[number - 1] for number in choices]
        except ValueError:
            print("Choose displayed job numbers, 'all', or 'q'.")


def print_status(session: dict | None) -> None:
    if session is None:
        print("No saved local queue session.")
        return
    print(f"Session {session['status']}, created {session['created_utc']}")
    for entry in session["entries"]:
        site = entry["manifest"]["site"]["name"] if session["schema_version"] == 1 else entry["site"]
        print(f"  {site}: run {entry['run_no'] or 'not started'}, {entry['status']}")
        if entry.get("error"):
            print(f"    {entry['error']}")
    if session["schema_version"] == 1:
        print(LEGACY_MESSAGE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Process ready shared queue jobs locally with resumable logs.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--resume", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    root = session_root()
    try:
        client = boto3.client("s3", region_name=env("AWS_REGION"))
        bucket = env("S3_BUCKET_NAME")
        if args.status:
            print_status(load_session(root))
            print_queue(queue_snapshot(client, bucket), hotspots.load())
            return 0
        with worker.local_lock(root):
            previous = load_session(root)
            if previous is not None and previous["schema_version"] == 1:
                print(LEGACY_MESSAGE)
                return 1
            if args.resume:
                if previous is None:
                    print("No saved queue session to resume.")
                    return 1
                return execute_session(previous, client, bucket, root, resume=True)
            unfinished = previous is not None and any(entry["status"] != "done" for entry in previous["entries"])
            if args.all and unfinished:
                print("An unfinished queue session exists. Use --resume, or choose New interactively.")
                return 1
            if previous is not None and not args.all:
                print_status(previous)
                options = "[r] Resume latest / [n] New / [q] Quit: " if unfinished else "[n] New / [q] Quit: "
                while True:
                    choice = input(options).strip().lower()
                    if choice in ("q", "quit"):
                        return 0
                    if choice in ("r", "resume") and unfinished:
                        return execute_session(previous, client, bucket, root, resume=True)
                    if choice in ("n", "new"):
                        break
            rows = queue_snapshot(client, bucket)
            print_queue(rows, hotspots.load())
            ready = [row for row in rows if row["status"] == "ready"]
            available = [
                row for row in rows
                if row["status"] == "ready" or row["status"] in ("claimed", "failed") and row.get("owned")
            ]
            if not (ready if args.all else available):
                print("No selectable shared jobs. Saved session and log retained; no catalogue search was started.")
                return 0
            selected = ready if args.all else ask_selection(available)
            if not selected:
                return 0
            session = create_queue_session(selected)
            save_session(session, root)
            (root / "session.log").write_text("", encoding="utf-8")
            return execute_session(session, client, bucket, root)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled. Saved session and log retained.")
        return 130
    except Exception as exc:
        print(f"Launcher stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
