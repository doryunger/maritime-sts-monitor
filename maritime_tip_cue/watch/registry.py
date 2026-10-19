import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from maritime_tip_cue.config import ROOT, settings


def runs_root() -> Path:
    return ROOT / settings()["runs"]["path"]


def connect() -> sqlite3.Connection:
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "registry.db", timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("create table if not exists runs (run_no integer primary key, folder text unique, created_utc text)")
    return db


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def created_of(folder: Path) -> str | None:
    if not (folder / "state.db").exists():
        return None
    db = sqlite3.connect(f"file:{(folder / 'state.db').as_posix()}?mode=ro", uri=True)
    try:
        return db.execute("select min(started_utc) from runs").fetchone()[0]
    finally:
        db.close()


def register(folder: Path) -> int:
    folder = folder.resolve()
    name = folder.relative_to(runs_root().resolve()).as_posix()
    db = connect()
    try:
        db.execute("begin immediate")
        row = db.execute("select run_no from runs where folder=?", (name,)).fetchone()
        if row is None:
            no = db.execute("select coalesce(max(run_no), 0) + 1 from runs").fetchone()[0]
            db.execute("insert into runs values (?,?,?)", (no, name, created_of(folder) or now()))
        else:
            no = row["run_no"]
        db.execute("commit")
        return no
    finally:
        db.close()


def new_run() -> tuple[int, Path]:
    db = connect()
    try:
        db.execute("begin immediate")
        no = db.execute("select coalesce(max(run_no), 0) + 1 from runs").fetchone()[0]
        db.execute("insert into runs values (?,?,?)", (no, str(no), now()))
        db.execute("commit")
    finally:
        db.close()
    folder = runs_root() / str(no)
    folder.mkdir(parents=True, exist_ok=True)
    return no, folder


def folder_of(no: int) -> Path:
    db = connect()
    try:
        row = db.execute("select folder from runs where run_no=?", (no,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise KeyError(f"run {no} is not registered")
    return runs_root() / row["folder"]


def listing() -> list[dict]:
    db = connect()
    try:
        return [dict(r) for r in db.execute("select * from runs order by run_no")]
    finally:
        db.close()


def running(folder: Path) -> bool:
    if not (folder / "state.db").exists():
        return False
    db = sqlite3.connect(f"file:{(folder / 'state.db').as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute("select ended_utc from runs order by run_id desc limit 1").fetchone()
        return row is not None and row[0] is None
    finally:
        db.close()


def adopt(no: int, force: bool = False) -> str:
    folder = folder_of(no)
    if folder.name == str(no):
        return f"run {no} already in {folder}"
    if running(folder) and not force:
        return f"run {no} ({folder.name}) has not finished; left as is"
    target = runs_root() / str(no)
    folder.rename(target)
    db = connect()
    try:
        db.execute("update runs set folder=? where run_no=?", (str(no), no))
    finally:
        db.close()
    return f"run {no}: {folder.name} -> {target.name}"


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.watch.registry")
    parser.add_argument("command", choices=("list", "register", "adopt"))
    parser.add_argument("target", nargs="*")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "register":
        folders = sorted((Path(t) for t in args.target), key=lambda f: created_of(f) or "")
        for f in folders:
            print(f"run {register(f)}: {f}")
    elif args.command == "adopt":
        nos = [int(t) for t in args.target] or [r["run_no"] for r in listing()]
        for no in nos:
            print(adopt(no, args.force))
    else:
        for r in listing():
            print(f"{r['run_no']:>4}  {r['created_utc']}  {r['folder']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
