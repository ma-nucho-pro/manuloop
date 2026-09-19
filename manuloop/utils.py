"""Small, dependency-free helpers used by ManuLOOP."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_id() -> str:
    return f"{utc_stamp()}-{secrets.token_hex(3)}"


def safe_name(value: str, fallback: str = "item") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return clean[:80] or fallback


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def truncate(value: str | None, limit: int = 12000) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return value[:limit] + f"\n...[{omitted} caracteres omitidos]..."


def compact_json(value: Any, limit: int = 20000) -> str:
    return truncate(json.dumps(value, ensure_ascii=False, indent=2), limit)


def _candidate_json_strings(text: str) -> Iterable[str]:
    """Yield likely JSON documents from plain, fenced, or mixed model output."""

    stripped = text.strip()
    if stripped:
        yield stripped

    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        candidate = match.group(1).strip()
        if candidate:
            yield candidate

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        candidate = text[index : index + end].strip()
        if candidate:
            yield candidate


def extract_json(text: str) -> Any | None:
    """Best-effort extraction of one JSON value from an agent response."""

    # Prefer a JSONL interpretation when several complete lines are JSON
    # objects. Provider event streams commonly contain progress followed by a
    # final result, and returning the first event would hide the result.
    line_values: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            line_values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if len(line_values) > 1:
        return line_values[-1]
    if len(line_values) == 1:
        return line_values[0]

    seen: set[str] = set()
    for candidate in _candidate_json_strings(text):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None


def flatten_content(value: Any) -> str:
    """Turn common provider response shapes into readable text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [flatten_content(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "result", "response", "message", "output"):
            if key in value:
                text = flatten_content(value[key])
                if text:
                    return text
        return ""
    return str(value)


def first_nonempty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""
