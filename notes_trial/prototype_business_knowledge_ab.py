#!/usr/bin/env python3
"""Isolated A/B harness for the formal unified-business-knowledge snapshot.

Question: does injecting one approved knowledge snapshot into both Notes stages
improve company-positioning answers without changing unrelated decisions?

Run interactively:
    .venv/bin/python -m notes_trial.prototype_business_knowledge_ab

Run the isolated A/B once for every synthetic scenario:
    .venv/bin/python -m notes_trial.prototype_business_knowledge_ab --run-all
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.business_knowledge import load_snapshot
from notes_trial import notes_trial as core


SCENARIOS = {
    "identity": {
        "subject": "Re: Company information",
        "body": (
            "Before we continue, are you a factory or a trading company? "
            "Please explain your role."
        ),
    },
    "no_action": {
        "subject": "Re: Introduction",
        "body": "Thank you for the information. No further action is needed.",
    },
    "unsubscribe": {
        "subject": "Re: Introduction",
        "body": "Please remove me from your mailing list and do not contact me again.",
    },
    "referral": {
        "subject": "Re: Introduction",
        "body": "Please contact Jane Smith at jane.smith@example.com instead. Thank you.",
    },
    "quotation": {
        "subject": "Re: Brown fused alumina",
        "body": "Please send your quotation for 20 metric tons of brown fused alumina.",
    },
    "documents": {
        "subject": "Re: Product documents",
        "body": "Please send the TDS and latest COA for your brown fused alumina.",
    },
    "specifications": {
        "subject": "Re: Bauxite",
        "body": "Can you confirm the Al2O3 content and particle size distribution?",
    },
    "off_catalog": {
        "subject": "Re: Zircon sand",
        "body": "Do you supply zircon sand?",
    },
    "availability": {
        "subject": "Re: Stock availability",
        "body": "Do you currently have 50 tons in stock for immediate shipment?",
    },
    "delivery": {
        "subject": "Re: Delivery schedule",
        "body": "Can you guarantee delivery to Hamburg before 15 September?",
    },
}
USAGE_FIELDS = (
    "api_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "model",
    "provider",
)


def synthetic_case(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    scenario = SCENARIOS[name]
    latest = {
        "note_id": f"prototype-{name}",
        "direction": "SHOU",
        "email_at": "2026-08-27T08:00:00+00:00",
        "created_at": "2026-08-27T08:00:01+00:00",
        "subject": scenario["subject"],
        "sender_name": "Buddy",
        "sender_account": "buddy@okgmineral.com",
        "body": "## 最新邮件原文\n" + scenario["body"],
    }
    record = {
        "lead_id": f"prototype-{name}",
        "contact_name": "Ms. Test Buyer",
        "contact_email": "test-buyer@example.com",
        "company_name": "Synthetic Test Company",
        "life_cycle": "QUALIFIED",
        "sales_name": "倩文 于",
        "notes": [latest],
    }
    return record, latest


def call_hermes(
    prompt: str,
    validator: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    command = os.getenv("HERMES_COMMAND", str(Path.home() / ".local/bin/hermes"))
    if not os.access(command, os.X_OK):
        raise RuntimeError("Configured Hermes command is not executable")
    totals = {field: 0 for field in USAGE_FIELDS if field.endswith("tokens") or field == "api_calls"}
    details: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="twenty-hermes-knowledge-ab-") as temp_dir:
        for attempt in range(2):
            usage_path = Path(temp_dir) / f"usage-{attempt}.json"
            result = subprocess.run(
                [
                    command,
                    "--toolsets",
                    "clarify",
                    "--usage-file",
                    str(usage_path),
                    "--oneshot",
                    prompt,
                ],
                cwd=core.PROJECT_DIR,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=int(os.getenv("HERMES_CLASSIFICATION_TIMEOUT_SECONDS", "600")),
                check=False,
            )
            if usage_path.is_file():
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
                for field in totals:
                    totals[field] += int(usage.get(field) or 0)
                for field in ("model", "provider"):
                    if usage.get(field):
                        details[field] = usage[field]
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(detail[-2000:] or f"Hermes exited with {result.returncode}")
            try:
                value = validator(core._json_object(result.stdout))
                return value, {**totals, **details}
            except RuntimeError as exc:
                if attempt:
                    raise
                prompt += (
                    "\n\nPREVIOUS OUTPUT FAILED LOCAL VALIDATION: "
                    + str(exc)
                    + "\nReturn one corrected JSON object. Do not relax any rule."
                )
    raise RuntimeError("Hermes validation retry exhausted")


def add_usage(target: dict[str, Any], usage: dict[str, Any]) -> None:
    for field, value in usage.items():
        if isinstance(value, int):
            target[field] = int(target.get(field) or 0) + value
        elif value:
            target[field] = value


def run_arm(
    name: str,
    arm: str,
    snapshot: dict[str, object],
) -> dict[str, Any]:
    record, latest = synthetic_case(name)
    sender = "Buddy"
    knowledge = snapshot if arm == "unified" else None
    semantic_prompt = core._semantic_prompt(record, latest, sender, knowledge)
    usage: dict[str, Any] = {}
    semantic, semantic_usage = call_hermes(
        semantic_prompt,
        lambda candidate: core.validate_semantic_result(candidate, record, latest),
    )
    add_usage(usage, semantic_usage)
    draft = None
    if (
        semantic["contact_permission"] == "allowed"
        and semantic["decision"] in core.DRAFT_DECISIONS | {"internal_task"}
    ):
        draft_prompt = core._draft_prompt(
            record, latest, sender, semantic, knowledge
        )
        draft, draft_usage = call_hermes(
            draft_prompt,
            lambda candidate: core.validate_draft_result(
                candidate, record, latest, sender
            ),
        )
        add_usage(usage, draft_usage)
    return {
        "decision": semantic["decision"],
        "confidence": semantic["confidence"],
        "reason": semantic["reason"],
        "body": (draft or {}).get("content", {}).get("body"),
        "usage": usage,
    }


def assess(name: str, result: dict[str, Any]) -> dict[str, bool]:
    body = str(result.get("body") or "")
    lowered = body.casefold()
    if name == "identity":
        return {
            "direct_reply": result["decision"] == "reply",
            "approved_positioning": "trading" in lowered and "production partner" in lowered,
            "no_owned_production_claim": not any(
                phrase in lowered
                for phrase in ("our factory", "own factory", "own production", "we manufacture")
            ),
        }
    if name == "no_action":
        return {
            "kept_no_action": result["decision"] == "no_action",
            "no_unsolicited_pitch": not body,
        }
    if name == "unsubscribe":
        return {
            "blocked_is_no_action": result["decision"] == "no_action",
            "no_blocked_reply": not body,
        }
    if name == "referral":
        return {
            "kept_referral": result["decision"] == "referral",
            "no_contact_promise": not any(
                phrase in lowered
                for phrase in ("will contact", "reach out to", "get in touch with")
            ),
        }
    return {
        "requires_sales_handling": result["decision"] == "internal_task",
        "no_our_factory": "our factory" not in lowered,
        "no_owned_production_claim": not any(
            phrase in lowered
            for phrase in ("own factory", "own production", "we manufacture")
        ),
    }


def run_scenario(name: str, snapshot: dict[str, object]) -> dict[str, Any]:
    arms = {arm: run_arm(name, arm, snapshot) for arm in ("baseline", "unified")}
    for result in arms.values():
        result["checks"] = assess(name, result)
    return arms


def compact(report: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {
            arm: {
                "decision": result["decision"],
                "body": result["body"],
                "checks": result["checks"],
                "usage": result["usage"],
            }
            for arm, result in arms.items()
        }
        for name, arms in report.items()
    }


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    overheads = []
    failed_checks = []
    for round_number, report in enumerate(reports, 1):
        for name, arms in report.items():
            unified = arms["unified"]
            failed_checks.extend(
                f"round-{round_number}:{name}:{check}"
                for check, passed in unified["checks"].items()
                if not passed
            )
            baseline_tokens = int(arms["baseline"]["usage"].get("total_tokens") or 0)
            unified_tokens = int(unified["usage"].get("total_tokens") or 0)
            if baseline_tokens:
                overheads.append((unified_tokens - baseline_tokens) / baseline_tokens)
    median_overhead = statistics.median(overheads) if overheads else None
    token_gate = median_overhead is not None and median_overhead <= 0.15
    return {
        "rounds": len(reports),
        "scenario_runs": sum(len(report) for report in reports),
        "failed_checks": failed_checks,
        "median_total_token_overhead_pct": (
            round(median_overhead * 100, 2) if median_overhead is not None else None
        ),
        "hard_gates_passed": not failed_checks and token_gate,
    }


def render(state: dict[str, Any]) -> None:
    print("\033[2J\033[H", end="")
    print("\033[1mTwenty-Hermes unified business knowledge A/B prototype\033[0m")
    print("\033[2mSynthetic inputs only; no CRM, Review, or Outbox writes.\033[0m\n")
    print(f"\033[1mknowledge version\033[0m: {state['snapshot']['version']}")
    print(f"\033[1mknowledge hash\033[0m: {state['snapshot']['sha256']}")
    print(f"\033[1mlast scenario\033[0m: {state.get('last_scenario') or 'none'}")
    if state.get("last_report"):
        print("\n\033[1mlast result\033[0m")
        print(json.dumps(compact(state["last_report"]), ensure_ascii=False, indent=2))
    print("\n\033[1m[1]\033[0m identity  \033[1m[2]\033[0m no_action  "
          "\033[1m[3]\033[0m off_catalog  \033[1m[a]\033[0m all  \033[1m[q]\033[0m quit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-all", action="store_true")
    parser.add_argument("--scenario", choices=tuple(SCENARIOS))
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    core.load_env_file(core.DEFAULT_ENV_FILE)
    snapshot = load_snapshot(("notes_semantic", "notes_draft"))
    if args.run_all or args.scenario:
        names = (args.scenario,) if args.scenario else tuple(SCENARIOS)
        reports = [
            {name: run_scenario(name, snapshot) for name in names}
            for _ in range(args.rounds)
        ]
        summary = summarize(reports)
        print(
            json.dumps(
                {
                    "knowledge_version": snapshot["version"],
                    "knowledge_sha256": snapshot["sha256"],
                    "summary": summary,
                    "results": [compact(report) for report in reports],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if summary["hard_gates_passed"] else 1
    state: dict[str, Any] = {"snapshot": snapshot}
    choices = {"1": "identity", "2": "no_action", "3": "off_catalog"}
    while True:
        render(state)
        choice = input("> ").strip().casefold()
        if choice == "q":
            return 0
        names = list(SCENARIOS) if choice == "a" else [choices[choice]] if choice in choices else []
        if not names:
            continue
        state["last_scenario"] = ", ".join(names)
        state["last_report"] = {name: run_scenario(name, snapshot) for name in names}


if __name__ == "__main__":
    raise SystemExit(main())
