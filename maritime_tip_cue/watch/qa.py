import argparse
import json
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from maritime_tip_cue.watch.report import export_flags, report_dir, write_qa
from maritime_tip_cue.watch.state import QA_COLUMNS, final_verdicts, upsert_flags


def handler(folder: Path):
    out = report_dir(folder)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(out), **k)

        def send_json(self, code: int, body) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/qa.html"):
                write_qa(folder)
                self.path = "/qa.html"
            if self.path == "/api/flags":
                return self.send_json(200, {"flags": export_flags(folder), "final": final_verdicts(folder)})
            return super().do_GET()

        def do_POST(self):
            if self.path != "/api/flags":
                return self.send_json(404, {"error": "not found"})
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not d.get("hull_id") or not d.get("status"):
                return self.send_json(400, {"error": "hull_id and status are required"})
            row = {k: d.get(k) for k in QA_COLUMNS} | {"reviewer": d.get("reviewer") or d.get("by") or "local"}
            upsert_flags(folder, [row])
            export_flags(folder)
            final = final_verdicts(folder)
            return self.send_json(200, {"flag": row, "final_verdict": final.get(d["hull_id"])})

        def log_message(self, fmt, *args):
            if self.command == "POST":
                print(f"saved {args[0] if args else ''}", flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(prog="maritime_tip_cue.watch.qa")
    parser.add_argument("folder")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    folder = Path(args.folder).resolve()
    if not (report_dir(folder) / "data.json").exists():
        print(f"no report for {folder.name}; run watch.report first")
        return 1
    write_qa(folder)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(folder))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"QA for {folder.name} at {url}, saving to {folder / 'state.db'} (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
