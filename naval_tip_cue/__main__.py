import argparse
import logging
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="naval_tip_cue")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb")
    pos = sub.add_parser("positions", help="convert a vessel-positions JSON snapshot to CSV/JSONL/GeoJSON")
    pos.add_argument("input", nargs="?", default="-", help="JSON file; '-' reads stdin")
    pos.add_argument("--format", choices=("csv", "jsonl", "geojson"), default="csv")
    pos.add_argument("--out", type=Path, help="output file; default stdout")
    pos.add_argument("--no-sat", action="store_true", help="drop anonymised [SAT-AIS] rows")
    pos.add_argument("--fetched", help="ISO time the snapshot was fetched (default now, UTC)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.command == "initdb":
        from naval_tip_cue.db import init_db

        init_db()
        logging.info("schema applied")
    elif args.command == "positions":
        from naval_tip_cue.positions import run

        run(args.input, args.format, args.out, args.no_sat, args.fetched)


if __name__ == "__main__":
    main()
