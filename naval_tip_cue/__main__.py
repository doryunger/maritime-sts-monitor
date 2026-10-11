import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="naval_tip_cue")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.command == "initdb":
        from naval_tip_cue.db import init_db

        init_db()
        logging.info("schema applied")


if __name__ == "__main__":
    main()
