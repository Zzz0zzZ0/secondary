import json
import os
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.outbox import get_review_message, replace_message_draft
from app.secondary.message_policy import message_route, validation_errors


PROJECT_DIR = Path(__file__).resolve().parent.parent
PIPELINE_SCRIPT = PROJECT_DIR / "scripts" / "twenty_run_hermes_pipeline.sh"
DEFAULT_RUNS_DIR = PROJECT_DIR / "outputs" / "review-runs"
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


def _validation_errors(candidate: Any, crm_input: Dict[str, Any], lead_id: str) -> List[str]:
    return (
        validation_errors(candidate, crm_input, lead_id)
        or ["未通过系统校验，需技术复核"]
    )


def pipeline_environment(
    limit: int,
    sort: str,
    run_root: Path,
    input_file: Optional[Path] = None,
) -> Dict[str, str]:
    env = _hermes_environment()
    env.pop("TWENTY_OUTPUT_CHANNEL", None)
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
        "next_action",
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


def load_run_results(run_root: Path) -> List[Dict[str, Any]]:
    pipeline_dir = _pipeline_dir(run_root)
    if pipeline_dir is None:
        return []
    summary = _read_json(pipeline_dir / "summary.json") or {}
    statuses = {
        item.get("lead_id"): item
        for item in summary.get("records", [])
        if isinstance(item, dict)
    }
    records = []
    for record_dir in sorted((pipeline_dir / "records").glob("*")):
        if not record_dir.is_dir():
            continue
        crm_input = _read_json(record_dir / "input.json") or {}
        output = _read_json(record_dir / "generated.json")
        candidate = _read_json(record_dir / "hermes_candidate.json")
        usage = _read_json(record_dir / "usage.json") or {}
        lead_id = str((crm_input.get("lead") or {}).get("id") or record_dir.name)
        status = statuses.get(lead_id, {})
        raw = ""
        if output is None:
            try:
                raw = (record_dir / "hermes_raw.txt").read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
        records.append(
            {
                "lead_id": lead_id,
                "pipeline_status": status.get("pipeline_status", "valid" if output else "processing"),
                "input": crm_input,
                "output": output,
                "candidate_output": candidate if output is None else None,
                "validation_errors": _validation_errors(candidate, crm_input, lead_id) if output is None else [],
                "usage": usage,
                "raw_output": raw,
            }
        )
    return records


@dataclass
class RunState:
    run_id: str
    limit: int
    channel: str
    sort: str
    source: str
    status: str
    run_root: str
    created_at: str
    total: int = 0
    completed: int = 0
    valid: int = 0
    invalid: int = 0
    error: Optional[str] = None


class RunManager:
    def __init__(self, runs_dir: Optional[Path] = None):
        self.runs_dir = (runs_dir or DEFAULT_RUNS_DIR).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._runs: Dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._active_run_id: Optional[str] = None

    def start(
        self,
        limit: int,
        sort: str,
        inputs: List[Dict[str, Any]],
        source: str = "scheduled",
    ) -> Dict[str, Any]:
        if not inputs:
            raise RuntimeError("当前没有可用于预览的已排期线索")
        with self._lock:
            if self._active_run_id:
                active = self._runs.get(self._active_run_id)
                if active and active.status == "running":
                    raise RuntimeError("已有测试任务正在运行")
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
            run_root = self.runs_dir / run_id
            run_root.mkdir(parents=True)
            input_file = run_root / "selected-inputs.json"
            input_file.write_text(
                json.dumps(inputs, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            input_file.chmod(0o600)
            state = RunState(
                run_id=run_id,
                limit=limit,
                channel="auto",
                sort=sort,
                source=source,
                status="running",
                run_root=str(run_root),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._runs[run_id] = state
            self._active_run_id = run_id
        threading.Thread(target=self._execute, args=(run_id,), daemon=True).start()
        return self.get(run_id)

    def _execute(self, run_id: str) -> None:
        state = self._runs[run_id]
        run_root = Path(state.run_root)
        log_path = run_root / "pipeline.log"
        try:
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    ["bash", str(PIPELINE_SCRIPT)],
                    cwd=PROJECT_DIR,
                    env=pipeline_environment(
                        state.limit,
                        state.sort,
                        run_root,
                        run_root / "selected-inputs.json",
                    ),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
            state.status = "completed" if result.returncode == 0 else "failed"
            if result.returncode != 0:
                lines = log_path.read_text(encoding="utf-8").splitlines()
                state.error = "\n".join(lines[-12:]) or f"生成流程退出码：{result.returncode}"
        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
        finally:
            self._refresh(state)
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None

    def _refresh(self, state: RunState) -> None:
        run_root = Path(state.run_root)
        pipeline_dir = _pipeline_dir(run_root)
        if pipeline_dir is None:
            return
        inputs = _read_json(pipeline_dir / "inputs.json")
        if isinstance(inputs, list):
            state.total = len(inputs)
        results_path = pipeline_dir / "results.jsonl"
        if results_path.exists():
            state.completed = sum(1 for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip())
        summary = _read_json(pipeline_dir / "summary.json")
        if isinstance(summary, dict):
            state.total = int(summary.get("record_count", state.total))
            state.valid = int(summary.get("valid_count", 0))
            state.invalid = int(summary.get("invalid_count", 0))
            state.completed = state.valid + state.invalid

    def get(self, run_id: str) -> Dict[str, Any]:
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        self._refresh(state)
        return asdict(state)

    def results(self, run_id: str) -> List[Dict[str, Any]]:
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        return load_run_results(Path(state.run_root))


manager = RunManager()
