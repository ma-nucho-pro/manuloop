"""Configuration loading and built-in provider profiles."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .utils import write_json


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "provider": "auto",
    "judge_provider": "auto",
    "max_rounds": 3,
    "preflight_rounds": 2,
    "scout_count": 4,
    "judge_count": 2,
    "timeout_seconds": 1800,
    "allow_edits": False,
    "allow_no_checks": False,
    "workspace_mode": "shared",
    "integration_pass": True,
    "artifacts_dir": ".manuloop/runs",
    "checks": [],
    "providers": {
        "claude": {
            "command": [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
                "{prompt}",
            ],
            "readonly_command": [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "plan",
                "{prompt}",
            ],
            "output": "json",
        },
        "codex": {
            "command": [
                "codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "--json",
                "{prompt}",
            ],
            "readonly_command": [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--json",
                "{prompt}",
            ],
            "output": "jsonl",
        },
        "gemini": {
            "command": [
                "gemini",
                "--prompt",
                "{prompt}",
                "--output-format",
                "json",
                "--approval-mode",
                "auto_edit",
            ],
            "readonly_command": [
                "gemini",
                "--prompt",
                "{prompt}",
                "--output-format",
                "json",
                "--approval-mode",
                "plan",
            ],
            "output": "json",
        },
        "custom": {
            "command": ["{prompt}"],
            "readonly_command": ["{prompt}"],
            "output": "text",
        },
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def default_config_path(workspace: Path) -> Path:
    return workspace / ".manuloop" / "config.json"


def load_config(workspace: Path, path: Path | None = None) -> dict[str, Any]:
    config_path = path or default_config_path(workspace)
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"La configuración debe ser un objeto JSON: {config_path}")
    return deep_merge(DEFAULT_CONFIG, raw)


def write_default_config(workspace: Path, path: Path | None = None, force: bool = False) -> Path:
    target = path or default_config_path(workspace)
    if target.exists() and not force:
        raise FileExistsError(f"Ya existe {target}; usa --force para reemplazarlo")
    write_json(target, DEFAULT_CONFIG)
    return target


def available_providers(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, profile in config.get("providers", {}).items():
        command = profile.get("command", []) if isinstance(profile, dict) else []
        executable = command[0] if command and isinstance(command[0], str) else ""
        found = False
        if executable and executable != "{prompt}":
            found = shutil.which(executable) is not None
        rows.append({"name": name, "executable": executable, "installed": found})
    return rows


def resolve_provider(name: str, config: dict[str, Any], exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    names = [name] if name != "auto" else ["codex", "claude", "gemini", "custom"]
    for candidate in names:
        if candidate in exclude:
            continue
        profile = config.get("providers", {}).get(candidate, {})
        command = profile.get("command", []) if isinstance(profile, dict) else []
        executable = command[0] if command else ""
        if candidate == "custom":
            # The default custom profile is a placeholder. Auto-detection must
            # not select it and then try to execute the prompt as a command.
            if executable and executable != "{prompt}" and shutil.which(executable):
                return candidate
            continue
        if executable and shutil.which(executable):
            return candidate
    if name != "auto":
        raise RuntimeError(f"No se encontró el ejecutable para el provider '{name}'")
    raise RuntimeError(
        "No se encontró Claude Code, Codex ni Gemini CLI. "
        "Instala uno o configura un provider custom en .manuloop/config.json."
    )
