"""Command-line entry point for checking and exporting a protocol book."""

import argparse
import json
from pathlib import Path
import sys

from .book import ProtocolBook, ProtocolError


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m tep_protocol",
        description="Check and export TEP protocol records for the pinned Studio revision.",
    )
    parser.add_argument("--path", type=Path, default=Path("data/protocol.json"), help="input book JSON path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate the book")
    show = commands.add_parser("show", help="print one record as JSON")
    show.add_argument("clause_id", help="short ID or the full versioned clause key")
    commands.add_parser("export", help="write one JSON record per line")
    args = parser.parse_args(argv)

    try:
        book = ProtocolBook.load(args.path)
        if args.command == "check":
            print(f"{book.book_id} · {len(book)} clauses validated")
        elif args.command == "show":
            print(json.dumps(book.show(args.clause_id), ensure_ascii=False, allow_nan=False, indent=2))
        else:
            for record in book.records():
                print(json.dumps(record, ensure_ascii=False, allow_nan=False))
    except ProtocolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
