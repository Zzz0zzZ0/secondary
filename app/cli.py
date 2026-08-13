import argparse
import json
import sys
from pathlib import Path

from .db import apply_migrations
from .outbox import (
    approve_message,
    get_message,
    import_run,
    list_messages,
    list_outbox,
    preflight as outbox_preflight,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent


def print_rows(rows):
    for row in rows:
        print(" | ".join(str(value) for value in row))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Twenty → Hermes → Outbox")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    import_parser = sub.add_parser("import-run")
    import_parser.add_argument("run_dir", type=Path)
    list_parser = sub.add_parser("list-messages")
    list_parser.add_argument("--limit", type=int, default=20)
    show_parser = sub.add_parser("show")
    show_parser.add_argument("message_id")
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("message_id")
    approve_parser.add_argument("--reviewer", required=True)
    approve_parser.add_argument("--subject")
    approve_parser.add_argument("--body-file", type=Path)
    approve_parser.add_argument("--note")
    approve_parser.add_argument("--yes", action="store_true")
    outbox_parser = sub.add_parser("list-outbox")
    outbox_parser.add_argument("--limit", type=int, default=20)
    sub.add_parser("outbox-preflight")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        for path in apply_migrations(PROJECT_DIR):
            print(f"Applied: {path}")
    elif args.command == "import-run":
        imported, skipped = import_run(args.run_dir)
        print(json.dumps({"imported": imported, "skipped": skipped}))
    elif args.command == "list-messages":
        print_rows(list_messages(args.limit))
    elif args.command == "show":
        row = get_message(args.message_id)
        if row is None:
            raise RuntimeError("Message not found")
        print(json.dumps({
            "id": str(row[0]), "lead_id": row[1], "recipient": row[2],
            "crm_snapshot": row[3], "original_output": row[4],
            "edited_output": row[5], "review_status": row[6],
        }, ensure_ascii=False, indent=2, default=str))
    elif args.command == "approve":
        if not args.yes:
            raise RuntimeError("Approval creates a sendable Outbox item; pass --yes after reviewing")
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else None
        outbox_id = approve_message(
            args.message_id, args.reviewer, args.subject, body, args.note
        )
        print(f"Approved and queued: {outbox_id}")
    elif args.command == "list-outbox":
        print_rows(list_outbox(args.limit))
    elif args.command == "outbox-preflight":
        print(json.dumps(outbox_preflight()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
