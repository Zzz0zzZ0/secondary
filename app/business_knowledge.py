"""Validated, compact snapshots of human-approved business knowledge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skill"
    / "generate-secondary-lead-message"
    / "references"
    / "business-knowledge.json"
)
USE_MODES = {"direct_answer", "internal_task"}


def load_snapshot(
    scopes: tuple[str, ...], path: Path = DEFAULT_PATH
) -> dict[str, Any]:
    if not scopes or any(not isinstance(scope, str) or not scope for scope in scopes):
        raise RuntimeError("Business knowledge scopes are required")
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Business knowledge cannot be loaded") from exc
    if set(source) != {"version", "entries"} or not isinstance(
        source["version"], str
    ) or not isinstance(source["entries"], list):
        raise RuntimeError("Business knowledge root is invalid")

    seen: set[str] = set()
    selected: list[dict[str, str]] = []
    known_scopes: set[str] = set()
    for entry in source["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "use_mode",
            "scopes",
            "text",
        }:
            raise RuntimeError("Business knowledge entry is invalid")
        entry_id = entry["id"]
        use_mode = entry["use_mode"]
        entry_scopes = entry["scopes"]
        text = entry["text"]
        if (
            not isinstance(entry_id, str)
            or not entry_id
            or entry_id in seen
            or use_mode not in USE_MODES
            or not isinstance(entry_scopes, list)
            or not entry_scopes
            or any(not isinstance(scope, str) or not scope for scope in entry_scopes)
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise RuntimeError("Business knowledge entry is invalid")
        seen.add(entry_id)
        known_scopes.update(entry_scopes)
        if any(scope in entry_scopes for scope in scopes):
            selected.append(
                {"id": entry_id, "use_mode": use_mode, "text": text.strip()}
            )
    if any(scope not in known_scopes for scope in scopes) or not selected:
        raise RuntimeError("Business knowledge scope is unsupported")

    identity = json.dumps(
        {"version": source["version"], "scopes": scopes, "entries": selected},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "version": source["version"],
        "scopes": scopes,
        "sha256": hashlib.sha256(identity).hexdigest(),
        "prompt_text": json.dumps(
            {"approved_business_knowledge": selected},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "routing": {
            mode: tuple(
                entry["id"] for entry in selected if entry["use_mode"] == mode
            )
            for mode in sorted(USE_MODES)
        },
    }
