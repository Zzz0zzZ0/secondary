import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.outbox import get_review_message, replace_message_draft
from app.secondary.message_policy import message_route


PROJECT_DIR = Path(__file__).resolve().parent.parent
PIPELINE_SCRIPT = PROJECT_DIR / "scripts" / "twenty_run_hermes_pipeline.sh"
DEFAULT_REGENERATION_DIR = (
    PROJECT_DIR / "outputs" / "review-regenerations"
)


def _hermes_environment() -> Dict[str, str]:
    """Copy the process environment and remove httpx-hostile IPv6 bypass tokens."""
    env = os.environ.copy()
    for key in ("NO_PROXY", "no_proxy"):
        value = env.get(key)
        if value:
            tokens = [token.strip() for token in value.split(",")]
            env[key] = ",".join(
                token for token in tokens if token not in {"::1", "::1/128"}
            )
    return env


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _pipeline_dir(run_root: Path) -> Optional[Path]:
    candidates = (
        sorted(
            path
            for path in run_root.iterdir()
            if path.is_dir()
            and (
                (path / "records").is_dir()
                or (path / "inputs.json").exists()
                or (path / "summary.json").exists()
            )
        )
        if run_root.exists()
        else []
    )
    return candidates[-1] if candidates else None


def pipeline_environment(
    limit: int,
    run_root: Path,
    input_file: Optional[Path] = None,
) -> Dict[str, str]:
    env = _hermes_environment()
    env.pop("HERMES_HUMAN_REVIEW_INSTRUCTION", None)
    env.pop("HERMES_REVIEW_CURRENT_DRAFT", None)
    env.update(
        {
            "TWENTY_EXPORT_LIMIT": str(limit),
            "TWENTY_HERMES_PROCESS_LIMIT": str(limit),
            "TWENTY_HERMES_RUNS_DIR": str(run_root),
            "TWENTY_HERMES_REPORTS_DIR": str(run_root / "reports"),
            "OUTBOX_AUTO_IMPORT": "false",
        }
    )
    if input_file is not None:
        env["TWENTY_HERMES_INPUT_FILE"] = str(input_file)
    return env


def regenerate_review_message(
    message_id: str,
    instruction: str,
    reviewer: str,
) -> Dict[str, Any]:
    instruction = instruction.strip()
    if not instruction:
        raise RuntimeError("Human review instruction cannot be empty")
    message = get_review_message(message_id)
    if message is None:
        raise KeyError(message_id)
    if message["review_status"] != "pending_review":
        raise RuntimeError(
            f"Message is already {message['review_status']}"
        )
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    regeneration_root = Path(
        os.getenv(
            "HERMES_REVIEW_REGENERATION_DIR",
            str(DEFAULT_REGENERATION_DIR),
        )
    ).resolve()
    run_root = regeneration_root / run_id
    run_root.mkdir(parents=True)
    run_root.chmod(0o700)
    input_file = run_root / "selected-inputs.json"
    crm_snapshot = json.loads(json.dumps(message["crm_snapshot"]))
    crm_snapshot["message_route"] = message_route(crm_snapshot, None)
    input_file.write_text(
        json.dumps(
            [crm_snapshot],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    input_file.chmod(0o600)
    audit_file = run_root / "review-instruction.json"
    audit_file.write_text(
        json.dumps(
            {
                "message_id": message_id,
                "reviewer": reviewer,
                "instruction": instruction,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    audit_file.chmod(0o600)
    environment = pipeline_environment(
        1,
        run_root,
        input_file,
    )
    environment["HERMES_HUMAN_REVIEW_INSTRUCTION"] = instruction
    environment["HERMES_REVIEW_CURRENT_DRAFT"] = json.dumps(
        message["effective_output"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        result = subprocess.run(
            ["bash", str(PIPELINE_SCRIPT)],
            cwd=PROJECT_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=int(
                os.getenv(
                    "HERMES_REVIEW_REGENERATION_TIMEOUT_SECONDS",
                    "600",
                )
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not regenerate message with Hermes: {exc}"
        ) from exc
    log_path = run_root / "pipeline.log"
    log_path.write_text(
        (result.stdout or "") + (result.stderr or ""),
        encoding="utf-8",
    )
    log_path.chmod(0o600)
    pipeline_dir = _pipeline_dir(run_root)
    summary = (
        _read_json(pipeline_dir / "summary.json")
        if pipeline_dir is not None
        else None
    )
    if (
        result.returncode != 0
        or not isinstance(summary, dict)
        or summary.get("record_count") != 1
        or summary.get("valid_count") != 1
    ):
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            detail[-2000:]
            or "Hermes regeneration did not produce one valid message"
        )
    record_dirs = sorted((pipeline_dir / "records").glob("*"))
    if len(record_dirs) != 1:
        raise RuntimeError(
            "Hermes regeneration must contain exactly one record"
        )
    output = _read_json(record_dirs[0] / "generated.json")
    if not isinstance(output, dict):
        raise RuntimeError("Hermes regenerated output is missing")
    updated = replace_message_draft(message_id, output)
    return {
        "run_id": run_id,
        "message": updated,
    }
