"""Deterministic project checks and workspace evidence."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .utils import truncate


@dataclass
class CheckResult:
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_check(command: str, workspace: Path, timeout_seconds: int = 1800) -> CheckResult:
    """Run one explicit user-supplied check.

    The command is intentionally executed through the platform shell because
    users commonly provide pipes, `&&`, npm scripts, or PowerShell commands.
    It is never taken automatically from an agent response; only commands in
    the user's config/CLI arguments reach this function.
    """

    started = time.monotonic()
    env = os.environ.copy()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=True,
            check=False,
            env=env,
        )
        return CheckResult(
            command=command,
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return CheckResult(
            command=command,
            ok=False,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:
        return CheckResult(
            command=command,
            ok=False,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
        )


def run_checks(
    commands: list[str], workspace: Path, timeout_seconds: int = 1800
) -> list[CheckResult]:
    return [run_check(command, workspace, timeout_seconds) for command in commands]


def git_evidence(workspace: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {"is_git": False, "status": "", "diff_stat": ""}
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if root.returncode != 0:
            return evidence
        evidence["is_git"] = True
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        evidence["status"] = status.stdout or ""
        evidence["diff_stat"] = diff_stat.stdout or ""
    except OSError as exc:
        evidence["error"] = str(exc)
    return evidence


def checks_evidence(results: list[CheckResult]) -> dict[str, Any]:
    return {
        "count": len(results),
        "passed": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
        "results": [result.to_dict() for result in results],
    }


def evidence_for_prompt(
    check_results: list[CheckResult], workspace: Path, limit: int = 16000
) -> str:
    payload = {
        "checks": checks_evidence(check_results),
        "git": git_evidence(workspace),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    return truncate(encoded, limit)
