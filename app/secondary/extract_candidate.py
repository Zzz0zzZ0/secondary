import argparse
import json
import sys
from pathlib import Path

from .json_output import parse_json_object


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--errors", required=True, type=Path)
    args = parser.parse_args(argv)

    raw = args.input.read_text(encoding="utf-8")
    try:
        candidate, _, _ = parse_json_object(raw)
    except (json.JSONDecodeError, RuntimeError) as exc:
        args.errors.write_text(
            json.dumps(
                [f"Hermes返回内容不是有效JSON：{exc}"],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1
    args.output.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
