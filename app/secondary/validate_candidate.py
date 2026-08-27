import argparse
import json
from pathlib import Path

from .json_output import parse_json_object
from .message_policy import normalize_candidate, validation_errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--candidate-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--errors", required=True, type=Path)
    args = parser.parse_args(argv)

    crm_input = json.loads(args.input.read_text(encoding="utf-8"))
    try:
        candidate, _, _ = parse_json_object(args.raw.read_text(encoding="utf-8"))
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
    args.candidate_output.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    normalized = normalize_candidate(candidate, crm_input)
    lead_id = str((crm_input.get("lead") or {}).get("id") or "")
    errors = validation_errors(normalized, crm_input, lead_id)
    args.errors.write_text(
        json.dumps(errors, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if errors:
        return 1
    args.output.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
