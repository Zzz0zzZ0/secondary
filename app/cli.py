import argparse
import json
from pathlib import Path

from .db import apply_migrations
from .outbox import import_run, preflight as outbox_preflight


PROJECT_DIR = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Twenty → Hermes → Outbox")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    import_parser = sub.add_parser("import-run")
    import_parser.add_argument("run_dir", type=Path)
    sub.add_parser("outbox-preflight")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        for path in apply_migrations(PROJECT_DIR):
            print(f"Applied: {path}")
    elif args.command == "import-run":
        imported, skipped = import_run(args.run_dir)
        print(json.dumps({"imported": imported, "skipped": skipped}))
    elif args.command == "outbox-preflight":
        print(json.dumps(outbox_preflight()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
