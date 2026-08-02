import json
from typing import Any, Dict, Tuple


def _strip_markdown_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip() in {"```", "```json"}:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_early_root_close(text: str) -> Tuple[Dict[str, Any], str]:
    decoder = json.JSONDecoder()
    prefix, end = decoder.raw_decode(text)
    suffix = text[end:].strip()
    if (
        not isinstance(prefix, dict)
        or not text[:end].rstrip().endswith("}")
        or not suffix.startswith(",")
        or not suffix.endswith("}")
    ):
        raise json.JSONDecodeError("Unsupported JSON tail", text, end)

    tail = json.loads('{"__prefix__":true' + suffix)
    if not isinstance(tail, dict):
        raise json.JSONDecodeError("JSON tail is not an object", text, end)
    tail.pop("__prefix__", None)
    if not tail or any(key in prefix for key in tail):
        raise json.JSONDecodeError("JSON tail has duplicate fields", text, end)

    repaired = text[: text[:end].rfind("}")] + suffix
    value = json.loads(repaired)
    if not isinstance(value, dict):
        raise json.JSONDecodeError("Repaired JSON is not an object", text, end)
    return value, repaired


def parse_json_object(raw: str) -> Tuple[Dict[str, Any], str, bool]:
    text = _strip_markdown_fence(raw)
    try:
        value = json.loads(text)
        repaired = False
    except json.JSONDecodeError as original_error:
        try:
            value, text = _repair_early_root_close(text)
            repaired = True
        except (json.JSONDecodeError, ValueError, TypeError):
            raise original_error
    if not isinstance(value, dict):
        raise RuntimeError("Hermes output must be a JSON object")
    return value, text, repaired
