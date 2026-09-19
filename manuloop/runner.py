"""The ManuLOOP state machine."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import resolve_provider
from .prompts import (
    builder_prompt,
    final_judge_prompt,
    fix_prompt,
    integration_prompt,
    plan_judge_prompt,
    plan_prompt,
    plan_revision_prompt,
    scout_prompt,
    workstream_judge_prompt,
)
from .providers import AgentResult, ProviderAdapter
from .utils import (
    extract_json,
    read_json,
    safe_name,
    truncate,
    utc_stamp,
    write_json,
    write_text,
    run_id,
)
from .verification import (
    CheckResult,
    checks_evidence,
    git_evidence,
    run_checks,
)


SCOUT_FOCUSES = [
    "arquitectura, estructura, puntos de entrada y ownership seguro",
    "pruebas existentes, errores, regresiones y casos límite",
    "rendimiento, seguridad, observabilidad y operación",
    "criterios específicos del dominio, UX y definición de calidad",
]


@dataclass
class RunnerOptions:
    provider: str = "auto"
    judge_provider: str = "auto"
    scout_count: int = 4
    judge_count: int = 2
    max_rounds: int = 3
    preflight_rounds: int = 2
    timeout_seconds: int = 1800
    allow_edits: bool = False
    allow_no_checks: bool = False
    integration_pass: bool = True
    checks: list[str] | None = None
    artifacts_dir: str = ".manuloop/runs"


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save_agent(self, result: AgentResult, prompt: str) -> Path:
        folder = self.root / "agents" / safe_name(f"{result.phase}-{result.agent_id}")
        with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            write_text(folder / "prompt.md", prompt)
            write_text(folder / "stdout.txt", result.stdout)
            write_text(folder / "stderr.txt", result.stderr)
            write_json(folder / "result.json", result.to_dict())
        return folder

    def save_text(self, name: str, value: str) -> Path:
        path = self.root / name
        with self._lock:
            write_text(path, value)
        return path

    def save_json(self, name: str, value: Any) -> Path:
        path = self.root / name
        with self._lock:
            write_json(path, value)
        return path


class ManuLoopRunner:
    def __init__(self, workspace: Path, config: dict[str, Any], options: RunnerOptions):
        self.workspace = workspace.resolve()
        self.config = config
        self.options = options
        self.run_id = run_id()
        artifacts_root = Path(options.artifacts_dir)
        if not artifacts_root.is_absolute():
            artifacts_root = self.workspace / artifacts_root
        self.artifacts = ArtifactStore(artifacts_root / self.run_id)
        self._state_lock = threading.RLock()
        self._agent_counter = 0
        self.state: dict[str, Any] = {
            "version": 1,
            "run_id": self.run_id,
            "status": "RUNNING",
            "phase": "created",
            "workspace": str(self.workspace),
            "goal": "",
            "bar": "",
            "domain": "general",
            "provider": None,
            "judge_provider": None,
            "events": 0,
            "workstreams": {},
            "last_judgments": [],
        }
        self._event_file = self.artifacts.root / "events.jsonl"
        self._adapter_cache: dict[tuple[str, bool], ProviderAdapter] = {}
        self.provider_name = resolve_provider(options.provider, config)
        if options.judge_provider == "auto":
            try:
                self.judge_provider_name = resolve_provider(
                    "auto", config, exclude={self.provider_name}
                )
            except RuntimeError:
                self.judge_provider_name = self.provider_name
        else:
            self.judge_provider_name = resolve_provider(options.judge_provider, config)
        self.state["provider"] = self.provider_name
        self.state["judge_provider"] = self.judge_provider_name
        self._save_state()

    def _save_state(self) -> None:
        with self._state_lock:
            write_json(self.artifacts.root / "state.json", self.state)

    def _record(self, event: str, **data: Any) -> None:
        payload = {"at": utc_stamp(), "event": event, **data}
        with self._state_lock:
            self.state["events"] += 1
            self.state["phase"] = event
            with self._event_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._save_state()

    def _adapter(self, provider_name: str, mutating: bool) -> ProviderAdapter:
        key = (provider_name, mutating)
        if key not in self._adapter_cache:
            profile = dict(self.config.get("providers", {}).get(provider_name, {}))
            profile["cwd"] = str(self.workspace)
            self._adapter_cache[key] = ProviderAdapter(
                provider_name,
                profile,
                timeout_seconds=self.options.timeout_seconds,
            )
        return self._adapter_cache[key]

    def _next_agent_id(self, prefix: str) -> str:
        with self._state_lock:
            self._agent_counter += 1
            return f"{prefix}-{self._agent_counter:03d}"

    def _run_agent(
        self,
        prompt: str,
        phase: str,
        prefix: str,
        provider_name: str | None = None,
        mutating: bool = False,
    ) -> AgentResult:
        provider_name = provider_name or self.provider_name
        if mutating and not self.options.allow_edits:
            return AgentResult(
                provider=provider_name,
                phase=phase,
                agent_id=self._next_agent_id(prefix),
                ok=False,
                exit_code=None,
                timed_out=False,
                command=[],
                stdout="",
                stderr="",
                text="",
                duration_seconds=0,
                error="Edición bloqueada: ejecuta con --allow-edits o allow_edits=true.",
            )

        agent_id = self._next_agent_id(prefix)
        self._record(
            "agent.started",
            phase=phase,
            agent_id=agent_id,
            provider=provider_name,
            mutating=mutating,
        )
        result = self._adapter(provider_name, mutating).run(
            prompt,
            phase=phase,
            agent_id=agent_id,
            mutating=mutating,
        )
        folder = self.artifacts.save_agent(result, prompt)
        self._record(
            "agent.finished",
            phase=phase,
            agent_id=agent_id,
            provider=provider_name,
            ok=result.ok,
            error=result.error,
            artifact=str(folder),
        )
        return result

    def _run_parallel(
        self,
        jobs: list[tuple[str, str, str, bool, str | None]],
    ) -> list[AgentResult]:
        if not jobs:
            return []
        results: list[AgentResult | None] = [None] * len(jobs)
        workers = min(len(jobs), max(1, self.options.scout_count, self.options.judge_count))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._run_agent,
                    prompt,
                    phase,
                    prefix,
                    provider_name,
                    mutating,
                ): index
                for index, (prompt, phase, prefix, mutating, provider_name) in enumerate(jobs)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [result for result in results if result is not None]

    def _parse_json_result(self, result: AgentResult, label: str) -> Any:
        value = extract_json(result.text) or extract_json(result.stdout)
        if value is None:
            self._record(
                "structured_output.invalid",
                label=label,
                agent_id=result.agent_id,
                error=result.error or "No se encontró JSON válido",
            )
            raise ValueError(
                f"{label}: el provider no devolvió JSON válido. "
                f"Revisa {self.artifacts.root / 'agents'}"
            )
        return value

    def _normalize_plan(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("El plan no es un objeto JSON")
        raw_workstreams = value.get("workstreams")
        if not isinstance(raw_workstreams, list) or not raw_workstreams:
            raise ValueError("El plan debe incluir al menos un workstream")
        workstreams: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_workstreams):
            if not isinstance(raw, dict):
                raise ValueError(f"Workstream {index + 1} no es un objeto")
            item = dict(raw)
            item_id = safe_name(str(item.get("id") or f"workstream-{index + 1}"))
            if item_id in seen:
                raise ValueError(f"ID de workstream duplicado: {item_id}")
            seen.add(item_id)
            item["id"] = item_id
            item["title"] = str(item.get("title") or item_id)
            item["objective"] = str(item.get("objective") or item["title"])
            for key in ("owned_paths", "done_when", "verification", "dependencies"):
                value_list = item.get(key, [])
                if isinstance(value_list, str):
                    value_list = [value_list]
                if not isinstance(value_list, list):
                    value_list = []
                item[key] = [str(value) for value in value_list]
            workstreams.append(item)

        ids = {item["id"] for item in workstreams}
        for item in workstreams:
            unknown = set(item["dependencies"]) - ids
            if unknown:
                raise ValueError(
                    f"{item['id']} depende de workstreams inexistentes: {sorted(unknown)}"
                )

        normalized = dict(value)
        normalized["quality_bar"] = str(
            value.get("quality_bar") or self.state["bar"] or "Criterio no especificado"
        )
        normalized["workstreams"] = workstreams
        for key in ("verification_commands", "risks", "assumptions"):
            item_list = normalized.get(key, [])
            if isinstance(item_list, str):
                item_list = [item_list]
            normalized[key] = [str(item) for item in item_list] if isinstance(item_list, list) else []
        return normalized

    def _topological_workstreams(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        workstreams = {item["id"]: item for item in plan["workstreams"]}
        pending = set(workstreams)
        ordered: list[dict[str, Any]] = []
        while pending:
            ready = sorted(
                item_id
                for item_id in pending
                if set(workstreams[item_id]["dependencies"]).isdisjoint(pending)
            )
            if not ready:
                raise ValueError("El plan contiene un ciclo de dependencias")
            for item_id in ready:
                ordered.append(workstreams[item_id])
                pending.remove(item_id)
        return ordered

    def _judgment(self, result: AgentResult, label: str) -> dict[str, Any]:
        try:
            value = self._parse_json_result(result, label)
        except ValueError as exc:
            return {
                "verdict": "BLOCKED",
                "confidence": 0,
                "checks": [],
                "critical_findings": [
                    {
                        "id": "invalid-judge-output",
                        "severity": "critical",
                        "evidence": str(exc),
                        "required_fix": "Devolver el contrato JSON de ManuLOOP.",
                    }
                ],
                "next_action": "Reintentar con un juez que respete el contrato.",
            }
        if not isinstance(value, dict):
            value = {}
        verdict = str(value.get("verdict", "BLOCKED")).upper()
        if verdict not in {"PASS", "FAIL", "BLOCKED"}:
            verdict = "BLOCKED"
        findings = value.get("critical_findings", [])
        if not isinstance(findings, list):
            findings = [
                {
                    "id": "malformed-findings",
                    "severity": "high",
                    "evidence": "critical_findings no es una lista",
                    "required_fix": "Corregir el formato del juez.",
                }
            ]
        return {
            "verdict": verdict,
            "confidence": value.get("confidence", 0),
            "checks": value.get("checks", []) if isinstance(value.get("checks", []), list) else [],
            "critical_findings": findings,
            "next_action": str(value.get("next_action", "")),
            "agent_id": result.agent_id,
            "provider": result.provider,
        }

    def _all_pass(self, judgments: list[dict[str, Any]]) -> bool:
        return bool(judgments) and all(item.get("verdict") == "PASS" for item in judgments)

    def _findings(self, judgments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for judgment in judgments:
            for finding in judgment.get("critical_findings", []):
                if isinstance(finding, dict):
                    findings.append(finding)
        return findings

    def _run_judges(
        self,
        prompts: list[str],
        phase: str,
        prefix: str,
    ) -> list[dict[str, Any]]:
        jobs = [
            (prompt, phase, f"{prefix}-{index + 1}", False, self.judge_provider_name)
            for index, prompt in enumerate(prompts)
        ]
        results = self._run_parallel(jobs)
        judgments = [
            self._judgment(result, f"{phase}-{index + 1}")
            for index, result in enumerate(results)
        ]
        self.state["last_judgments"] = judgments
        self.artifacts.save_json(
            f"{safe_name(phase)}-judgments.json",
            judgments,
        )
        self._record(
            "judges.completed",
            phase=phase,
            verdicts=[judgment.get("verdict") for judgment in judgments],
        )
        return judgments

    def _scout(self, goal: str, bar: str, domain: str) -> list[str]:
        count = max(1, min(self.options.scout_count, len(SCOUT_FOCUSES)))
        jobs = [
            (
                scout_prompt(goal, bar, domain, SCOUT_FOCUSES[index]),
                "scouting",
                f"scout-{index + 1}",
                False,
                self.provider_name,
            )
            for index in range(count)
        ]
        results = self._run_parallel(jobs)
        reports = [result.text or result.error or "(sin respuesta)" for result in results]
        self.artifacts.save_json(
            "scout-reports.json",
            [
                {
                    "agent_id": result.agent_id,
                    "ok": result.ok,
                    "report": result.text,
                    "error": result.error,
                }
                for result in results
            ],
        )
        self._record("scouting.completed", count=len(reports))
        return reports

    def _preflight(
        self, goal: str, bar: str, domain: str
    ) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
        self._record("preflight.started")
        scouts = self._scout(goal, bar, domain)
        previous_findings: list[dict[str, Any]] = []
        plan: dict[str, Any] | None = None
        judgments: list[dict[str, Any]] = []
        for attempt in range(1, max(1, self.options.preflight_rounds) + 1):
            planner = self._run_agent(
                plan_prompt(goal, bar, domain, scouts, previous_findings),
                "planning",
                f"planner-{attempt}",
                provider_name=self.provider_name,
                mutating=False,
            )
            try:
                plan = self._normalize_plan(
                    self._parse_json_result(planner, f"planning-{attempt}")
                )
            except ValueError as exc:
                previous_findings = [
                    {
                        "id": "invalid-plan",
                        "severity": "critical",
                        "evidence": str(exc),
                        "required_fix": "Devolver un plan JSON completo.",
                    }
                ]
                self._record("preflight.plan_invalid", attempt=attempt, error=str(exc))
                continue

            self.artifacts.save_json(f"plan-{attempt}.json", plan)
            judge_prompts = [
                plan_judge_prompt(goal, bar, domain, plan, scouts, index + 1)
                for index in range(max(1, self.options.judge_count))
            ]
            judgments = self._run_judges(judge_prompts, "plan-judging", f"plan-judge-{attempt}")
            if self._all_pass(judgments):
                self._record("preflight.approved", attempt=attempt)
                return plan, scouts, judgments
            previous_findings = self._findings(judgments)
            if attempt < self.options.preflight_rounds:
                revision = self._run_agent(
                    plan_revision_prompt(goal, bar, domain, plan, previous_findings),
                    "planning-revision",
                    f"planner-revision-{attempt}",
                    provider_name=self.provider_name,
                    mutating=False,
                )
                try:
                    plan = self._normalize_plan(
                        self._parse_json_result(revision, f"planning-revision-{attempt}")
                    )
                    self.artifacts.save_json(f"plan-revision-{attempt}.json", plan)
                except ValueError as exc:
                    previous_findings.append(
                        {
                            "id": "invalid-plan-revision",
                            "severity": "critical",
                            "evidence": str(exc),
                            "required_fix": "Corregir la revisión del plan.",
                        }
                    )
        self._record(
            "preflight.blocked",
            findings=previous_findings,
        )
        return plan, scouts, judgments

    def _workstream_evidence(
        self, checks: list[CheckResult], worker: AgentResult | None = None
    ) -> str:
        payload = {
            "checks": checks_evidence(checks),
            "git": git_evidence(self.workspace),
        }
        if worker is not None:
            payload["builder_report"] = truncate(worker.text, 10000)
            payload["builder_error"] = worker.error
        return truncate(json.dumps(payload, ensure_ascii=False, indent=2), 20000)

    def _workstream_loop(
        self,
        goal: str,
        bar: str,
        domain: str,
        plan: dict[str, Any],
        workstream: dict[str, Any],
        scouts: list[str],
    ) -> bool:
        workstream_id = workstream["id"]
        self.state["workstreams"].setdefault(workstream_id, {})
        findings: list[dict[str, Any]] = []
        evidence = ""
        worker: AgentResult | None = None
        for round_number in range(1, max(1, self.options.max_rounds) + 1):
            self.state["workstreams"][workstream_id].update(
                {"status": "BUILDING", "round": round_number}
            )
            self._save_state()
            if round_number == 1:
                prompt = builder_prompt(goal, bar, domain, plan, workstream, scouts, evidence)
                phase = "building"
                prefix = f"builder-{workstream_id}"
            else:
                prompt = fix_prompt(
                    goal, bar, domain, plan, workstream, findings, evidence
                )
                phase = "fixing"
                prefix = f"fixer-{workstream_id}-{round_number}"
            worker = self._run_agent(
                prompt,
                phase,
                prefix,
                provider_name=self.provider_name,
                mutating=True,
            )
            checks = run_checks(
                self.options.checks or [],
                self.workspace,
                timeout_seconds=self.options.timeout_seconds,
            )
            self.artifacts.save_json(
                f"checks-{safe_name(workstream_id)}-{round_number}.json",
                [check.to_dict() for check in checks],
            )
            evidence = self._workstream_evidence(checks, worker)
            self.artifacts.save_text(
                f"evidence-{safe_name(workstream_id)}-{round_number}.json",
                evidence,
            )
            judge_prompts = [
                workstream_judge_prompt(
                    goal,
                    bar,
                    domain,
                    plan,
                    workstream,
                    evidence,
                    worker.text or worker.error or "",
                    index + 1,
                )
                for index in range(max(1, self.options.judge_count))
            ]
            judgments = self._run_judges(
                judge_prompts,
                f"workstream-{workstream_id}-judging",
                f"workstream-{workstream_id}-judge-{round_number}",
            )
            self.state["workstreams"][workstream_id]["last_judgments"] = judgments
            if self._all_pass(judgments) and all(check.ok for check in checks):
                self.state["workstreams"][workstream_id]["status"] = "PASSED"
                self._record("workstream.passed", workstream=workstream_id, round=round_number)
                return True
            findings = self._findings(judgments)
            if not findings and not all(check.ok for check in checks):
                findings = [
                    {
                        "id": "deterministic-check-failed",
                        "severity": "high",
                        "evidence": evidence,
                        "required_fix": "Corregir todas las comprobaciones fallidas.",
                    }
                ]
            self._record(
                "workstream.rejected",
                workstream=workstream_id,
                round=round_number,
                findings=findings,
            )
        self.state["workstreams"][workstream_id]["status"] = "BLOCKED"
        self.state["workstreams"][workstream_id]["findings"] = findings
        self._record("workstream.blocked", workstream=workstream_id, findings=findings)
        return False

    def _integration_loop(
        self, goal: str, bar: str, domain: str, plan: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]], list[CheckResult], str]:
        checks: list[CheckResult] = []
        evidence = self._workstream_evidence(checks)
        final_judgments: list[dict[str, Any]] = []
        for round_number in range(1, max(1, self.options.max_rounds) + 1):
            if round_number == 1:
                prompt = integration_prompt(goal, bar, domain, plan, evidence)
                phase = "integration"
                prefix = f"integrator-{round_number}"
            else:
                synthetic_workstream = {
                    "id": "integration",
                    "title": "Integración global",
                    "objective": goal,
                    "owned_paths": ["."],
                    "done_when": [bar],
                    "verification": self.options.checks or [],
                    "dependencies": [],
                }
                prompt = fix_prompt(
                    goal,
                    bar,
                    domain,
                    plan,
                    synthetic_workstream,
                    self._findings(final_judgments),
                    evidence,
                )
                phase = "integration-fix"
                prefix = f"integrator-fix-{round_number}"
            worker = self._run_agent(
                prompt,
                phase,
                prefix,
                provider_name=self.provider_name,
                mutating=True,
            )
            checks = run_checks(
                self.options.checks or [],
                self.workspace,
                timeout_seconds=self.options.timeout_seconds,
            )
            evidence = self._workstream_evidence(checks, worker)
            self.artifacts.save_json(
                f"integration-checks-{round_number}.json",
                [check.to_dict() for check in checks],
            )
            self.artifacts.save_text(f"integration-evidence-{round_number}.json", evidence)
            prompts = [
                final_judge_prompt(
                    goal, bar, domain, plan, evidence, index + 1
                )
                for index in range(max(1, self.options.judge_count))
            ]
            final_judgments = self._run_judges(
                prompts, "final-judging", f"final-judge-{round_number}"
            )
            if self._all_pass(final_judgments) and all(check.ok for check in checks):
                self._record("integration.passed", round=round_number)
                return True, final_judgments, checks, evidence
            self._record(
                "integration.rejected",
                round=round_number,
                findings=self._findings(final_judgments),
            )
        return False, final_judgments, checks, evidence

    def run_plan(
        self, goal: str, bar: str = "", domain: str = "general"
    ) -> dict[str, Any]:
        self.state.update({"goal": goal, "bar": bar, "domain": domain, "status": "RUNNING"})
        self._save_state()
        self.artifacts.save_json(
            "request.json",
            {"goal": goal, "bar": bar, "domain": domain, "mode": "plan"},
        )
        try:
            plan, scouts, judgments = self._preflight(goal, bar, domain)
            if plan is not None:
                self.state["plan"] = plan
            if plan is not None and self._all_pass(judgments):
                self.state["status"] = "PLAN_APPROVED"
                self._record("run.plan_approved")
            else:
                self.state["status"] = "BLOCKED"
                self._record("run.plan_blocked")
            self._save_state()
        except Exception as exc:
            self.state["status"] = "ERROR"
            self.state["error"] = str(exc)
            self._record("run.error", error=str(exc))
        return self.summary()

    def run(self, goal: str, bar: str = "", domain: str = "general") -> dict[str, Any]:
        self.state.update({"goal": goal, "bar": bar, "domain": domain, "status": "RUNNING"})
        self._save_state()
        self.artifacts.save_json(
            "request.json",
            {"goal": goal, "bar": bar, "domain": domain, "mode": "run"},
        )
        try:
            plan, scouts, judgments = self._preflight(goal, bar, domain)
            if plan is None or not self._all_pass(judgments):
                self.state["status"] = "BLOCKED"
                self.state["error"] = (
                    "El plan no fue aprobado por todos los jueces; no se hicieron cambios."
                )
                self._record("run.blocked.before_edits", reason=self.state["error"])
                return self.summary()

            self.state["plan"] = plan
            effective_bar = str(plan.get("quality_bar") or bar)
            if not self.options.allow_edits:
                self.state["status"] = "PLAN_APPROVED"
                self.state["error"] = (
                    "Plan aprobado, pero la ejecución requiere --allow-edits."
                )
                self._record("run.stopped_before_edits", reason=self.state["error"])
                return self.summary()
            if not (self.options.checks or []) and not self.options.allow_no_checks:
                self.state["status"] = "BLOCKED"
                self.state["error"] = (
                    "No hay comprobaciones deterministas. Añade --check o "
                    "usa --allow-no-checks aceptando evidencia más débil."
                )
                self._record("run.blocked.no_checks", reason=self.state["error"])
                return self.summary()

            ordered = self._topological_workstreams(plan)
            self._record(
                "implementation.started",
                workstreams=[item["id"] for item in ordered],
            )
            for workstream in ordered:
                if not self._workstream_loop(
                    goal, effective_bar, domain, plan, workstream, scouts
                ):
                    self.state["status"] = "BLOCKED"
                    self.state["error"] = (
                        f"El workstream '{workstream['id']}' no pasó el gate de jueces."
                    )
                    self._record(
                        "run.blocked.workstream",
                        workstream=workstream["id"],
                        reason=self.state["error"],
                    )
                    return self.summary()

            if self.options.integration_pass:
                passed, final_judgments, checks, evidence = self._integration_loop(
                    goal, effective_bar, domain, plan
                )
                self.state["final_judgments"] = final_judgments
                self.state["final_checks"] = [check.to_dict() for check in checks]
                self.state["final_evidence"] = evidence
                if not passed:
                    self.state["status"] = "BLOCKED"
                    self.state["error"] = "La integración o el juez final no pasó."
                    self._record("run.blocked.final_gate", reason=self.state["error"])
                    return self.summary()
            else:
                checks = run_checks(
                    self.options.checks or [],
                    self.workspace,
                    timeout_seconds=self.options.timeout_seconds,
                )
                self.state["final_checks"] = [check.to_dict() for check in checks]
                if not all(check.ok for check in checks):
                    self.state["status"] = "BLOCKED"
                    self.state["error"] = "Falló la comprobación final."
                    self._record("run.blocked.final_checks", reason=self.state["error"])
                    return self.summary()

            all_checks = self.state.get("final_checks", [])
            if all_checks and not all(item.get("ok") for item in all_checks):
                self.state["status"] = "BLOCKED"
                self.state["error"] = "La evidencia final contiene comprobaciones fallidas."
                self._record("run.blocked.final_checks", reason=self.state["error"])
                return self.summary()
            if not all_checks and self.options.allow_no_checks:
                self.state["status"] = "PASSED_WITHOUT_CHECKS"
            else:
                self.state["status"] = "PASSED"
            self._record("run.passed", status=self.state["status"])
        except Exception as exc:
            self.state["status"] = "ERROR"
            self.state["error"] = str(exc)
            self._record("run.error", error=str(exc))
        self._save_state()
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.state.get("status"),
            "phase": self.state.get("phase"),
            "artifact_dir": str(self.artifacts.root),
            "provider": self.provider_name,
            "judge_provider": self.judge_provider_name,
            "error": self.state.get("error"),
            "workstreams": self.state.get("workstreams", {}),
            "last_judgments": self.state.get("last_judgments", []),
        }


def load_run_summary(path: Path) -> dict[str, Any]:
    state_path = path / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(state_path)
    return read_json(state_path)
