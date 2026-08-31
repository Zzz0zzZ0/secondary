"""PROTOTYPE: build one deterministic prompt snapshot from approved facts."""

from __future__ import annotations

import hashlib
import json


def build_snapshot(
    version: str,
    scopes: tuple[str, ...],
    entries: tuple[dict[str, str], ...],
) -> dict[str, object]:
    """Return the portable logic being tested; callers own all I/O."""
    modes = {"direct_answer", "internal_task"}
    if (
        not version.strip()
        or not scopes
        or not entries
        or any(
            set(entry) != {"id", "use_mode", "text"}
            or entry["use_mode"] not in modes
            or not entry["id"].strip()
            or not entry["text"].strip()
            for entry in entries
        )
    ):
        raise ValueError("version, scopes, and valid approved entries are required")
    routing = {
        mode: tuple(entry["id"] for entry in entries if entry["use_mode"] == mode)
        for mode in sorted(modes)
    }
    identity = json.dumps(
        {
            "version": version,
            "scopes": scopes,
            "entries": entries,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "version": version,
        "scopes": scopes,
        "sha256": hashlib.sha256(identity).hexdigest(),
        "prompt_text": json.dumps(
            {"approved_business_knowledge": entries},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "routing": routing,
    }
