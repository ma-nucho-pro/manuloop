"""Provider-neutral subprocess adapters.

The orchestrator deliberately speaks a tiny protocol: send a prompt, capture a
result, and keep the raw receipt. Provider-specific flags stay in configuration
so a CLI can evolve without forcing changes in the loop engine.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

from .utils import extract_json, first_nonempty, flatten_content, truncate


@dataclass
class AgentResult:
    provider: str
    phase: str
    agent_id: str
    ok: bool
    exit_code: int | None
    timed_out: bool
    command: list[str]
    stdout: str
    stderr: str
    text: str
    duration_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event_text(value: Any) -> str:
    if not isinstance(value, dict):
        return flatten_content(value)
    item = value.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if item_type in {"agent_message", "message", "assistant"}:
            return flatten_content(item.get("text") or item.get("content") or item)
    if value.get("type") in {"message", "assistant", "result"}:
        return flatten_content(value.get("text") or value.get("content") or value.get("result"))
    return ""


def parse_provider_text(stdout: str, provider_profile: dict[str, Any]) -> str:
    """Extract final human-readable text from text, JSON, or JSONL output."""

    output_mode = provider_profile.get("output", "text")
    if output_mode == "text":
        return stdout.strip()

    if output_mode == "jsonl":
        messages: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = __import__("json").loads(line)
            except ValueError:
                continue
            text = _event_text(event)
            if text:
                messages.append(text)
            if isinstance(event, dict) and event.get("type") == "result":
                final = flatten_content(event.get("result") or event.get("response") or event)
                if final:
                    messages.append(final)
        if messages:
            # Keep order but avoid repeating the same result event.
            unique: list[str] = []
            for message in messages:
                if not unique or message != unique[-1]:
                    unique.append(message)
            return "\n".join(unique).strip()

    value = extract_json(stdout)
    if value is not None:
        if isinstance(value, dict):
            for key in ("response", "result", "text", "message", "content", "output"):
                text = flatten_content(value.get(key))
                if text:
                    return text.strip()
        text = flatten_content(value)
        if text:
            return text.strip()

    return stdout.strip()


class ProviderAdapter:
    def __init__(self, name: str, profile: dict[str, Any], timeout_seconds: int = 1800):
        self.name = name
        self.profile = profile
        self.timeout_seconds = timeout_seconds

    def _template(self, mutating: bool) -> list[str]:
        key = "command" if mutating else "readonly_command"
        template = self.profile.get(key) or self.profile.get("command")
        if not isinstance(template, list) or not template:
            raise ValueError(f"Provider '{self.name}' no tiene un command válido")
        return [str(token) for token in template]

    def build_command(self, prompt: str, mutating: bool) -> tuple[list[str], str | None]:
        command: list[str] = []
        prompt_consumed = False
        for token in self._template(mutating):
            if token == "{prompt}":
                command.append(prompt)
                prompt_consumed = True
            else:
                command.append(token)
        return command, None if prompt_consumed else prompt

    def run(self, prompt: str, phase: str, agent_id: str, mutating: bool) -> AgentResult:
        command, stdin_text = self.build_command(prompt, mutating=mutating)
        env = os.environ.copy()
        configured_env = self.profile.get("env", {})
        if isinstance(configured_env, dict):
            env.update({str(key): str(value) for key, value in configured_env.items()})

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=self.profile.get("cwd") or None,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            text = parse_provider_text(stdout, self.profile)
            ok = completed.returncode == 0
            return AgentResult(
                provider=self.name,
                phase=phase,
                agent_id=agent_id,
                ok=ok,
                exit_code=completed.returncode,
                timed_out=False,
                command=command,
                stdout=stdout,
                stderr=stderr,
                text=text,
                duration_seconds=time.monotonic() - started,
                error=None if ok else first_nonempty(stderr, text, "El provider terminó con error"),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return AgentResult(
                provider=self.name,
                phase=phase,
                agent_id=agent_id,
                ok=False,
                exit_code=None,
                timed_out=True,
                command=command,
                stdout=stdout,
                stderr=stderr,
                text=parse_provider_text(stdout, self.profile),
                duration_seconds=time.monotonic() - started,
                error=f"Timeout tras {self.timeout_seconds}s",
            )
        except (OSError, ValueError) as exc:
            return AgentResult(
                provider=self.name,
                phase=phase,
                agent_id=agent_id,
                ok=False,
                exit_code=None,
                timed_out=False,
                command=command,
                stdout="",
                stderr="",
                text="",
                duration_seconds=time.monotonic() - started,
                error=str(exc),
            )


def command_for_display(command: list[str]) -> str:
    """Return a readable command without attempting to execute a shell string."""

    import shlex

    return shlex.join([truncate(token, 240) for token in command])
