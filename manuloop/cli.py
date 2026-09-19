"""Command-line interface for ManuLOOP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    available_providers,
    load_config,
    write_default_config,
)
from .prompts import portable_prompt
from .runner import ManuLoopRunner, RunnerOptions, load_run_summary
from .utils import write_text


def _workspace(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise argparse.ArgumentTypeError(f"No es un directorio válido: {path}")
    return path


def _goal(args: argparse.Namespace) -> str:
    if getattr(args, "goal_file", None):
        return Path(args.goal_file).expanduser().read_text(encoding="utf-8").strip()
    goal = getattr(args, "goal", None)
    if not goal:
        raise SystemExit("Falta --goal o --goal-file")
    return goal.strip()


def _add_goal_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--goal", help="Objetivo en texto.")
    group.add_argument("--goal-file", help="Archivo UTF-8 con el objetivo.")
    parser.add_argument(
        "--bar",
        default="",
        help="Barra de calidad medible. Ej.: 'pytest, typecheck y sin errores de runtime'.",
    )
    parser.add_argument(
        "--domain",
        default="general",
        help="Dominio para ajustar los jueces: code, game, web, docs, data, etc.",
    )


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=_workspace, default=Path.cwd())
    parser.add_argument("--config", type=Path, help="Ruta alternativa a config.json.")
    parser.add_argument("--provider", default=None, help="claude, codex, gemini o custom.")
    parser.add_argument("--judge-provider", default=None, help="Provider independiente para jueces.")
    parser.add_argument("--scouts", type=int, default=None, help="Número de exploradores.")
    parser.add_argument("--judges", type=int, default=None, help="Número de jueces por gate.")
    parser.add_argument("--max-rounds", type=int, default=None, help="Rondas builder/fix por workstream.")
    parser.add_argument("--preflight-rounds", type=int, default=None, help="Rondas de validación del plan.")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout por agente/comprobación en segundos.")
    parser.add_argument(
        "--check",
        action="append",
        default=None,
        help="Comprobación determinista explícita; se puede repetir.",
    )
    parser.add_argument(
        "--allow-edits",
        action="store_true",
        default=None,
        help="Permite que los builders/fixers modifiquen el workspace.",
    )
    parser.add_argument(
        "--allow-no-checks",
        action="store_true",
        default=None,
        help="Permite terminar sin comandos de verificación deterministas (evidencia débil).",
    )
    parser.add_argument(
        "--no-integration-pass",
        action="store_true",
        help="Omite el pase holístico final; se desaconseja para juegos y cambios complejos.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Directorio base para recibos y estado; por defecto .manuloop/runs.",
    )
    parser.add_argument("--json", action="store_true", help="Imprime el resumen como JSON.")


def _options(args: argparse.Namespace, config: dict[str, Any]) -> RunnerOptions:
    def value(name: str, default: Any) -> Any:
        arg = getattr(args, name, None)
        return default if arg is None else arg

    checks = args.check if getattr(args, "check", None) else list(config.get("checks", []))
    return RunnerOptions(
        provider=value("provider", config.get("provider", "auto")),
        judge_provider=value("judge_provider", config.get("judge_provider", "auto")),
        scout_count=max(1, value("scouts", config.get("scout_count", 4))),
        judge_count=max(1, value("judges", config.get("judge_count", 2))),
        max_rounds=max(1, value("max_rounds", config.get("max_rounds", 3))),
        preflight_rounds=max(1, value("preflight_rounds", config.get("preflight_rounds", 2))),
        timeout_seconds=max(1, value("timeout", config.get("timeout_seconds", 1800))),
        allow_edits=bool(value("allow_edits", config.get("allow_edits", False))),
        allow_no_checks=bool(value("allow_no_checks", config.get("allow_no_checks", False))),
        integration_pass=not bool(getattr(args, "no_integration_pass", False))
        and bool(config.get("integration_pass", True)),
        checks=checks,
        artifacts_dir=value("artifacts_dir", config.get("artifacts_dir", ".manuloop/runs")),
    )


def _print(value: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def _summary_text(summary: dict[str, Any]) -> str:
    status = summary.get("status", "UNKNOWN")
    lines = [
        f"ManuLOOP: {status}",
        f"run_id: {summary.get('run_id', '-')}",
        f"provider: {summary.get('provider', '-')} | juez: {summary.get('judge_provider', '-')}",
        f"recibos: {summary.get('artifact_dir', '-')}",
    ]
    if summary.get("error"):
        lines.append(f"motivo: {summary['error']}")
    workstreams = summary.get("workstreams") or {}
    if workstreams:
        lines.append("workstreams:")
        for key, value in workstreams.items():
            lines.append(f"  - {key}: {value.get('status', 'UNKNOWN')}")
    return "\n".join(lines)


def _latest_run(workspace: Path, artifacts_dir: str) -> Path:
    root = Path(artifacts_dir)
    if not root.is_absolute():
        root = workspace / root
    candidates = [path for path in root.iterdir() if path.is_dir() and (path / "state.json").exists()]
    if not candidates:
        raise FileNotFoundError(f"No hay runs en {root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manuloop",
        description="Loop build -> verify -> judge -> fix para agentes de código y otros harnesses.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Crea .manuloop/config.json.")
    init_parser.add_argument("--workspace", type=_workspace, default=Path.cwd())
    init_parser.add_argument("--config", type=Path)
    init_parser.add_argument("--force", action="store_true")

    providers_parser = subparsers.add_parser("providers", help="Muestra providers configurados y detectados.")
    providers_parser.add_argument("--workspace", type=_workspace, default=Path.cwd())
    providers_parser.add_argument("--config", type=Path)
    providers_parser.add_argument("--json", action="store_true")

    prompt_parser = subparsers.add_parser("prompt", help="Genera un prompt portable para cualquier harness.")
    _add_goal_arguments(prompt_parser)
    prompt_parser.add_argument("--output", type=Path, help="Guarda el prompt en este archivo.")

    plan_parser = subparsers.add_parser(
        "plan",
        help="Ejecuta exploradores + plan + jueces, sin permitir modificaciones.",
    )
    _add_goal_arguments(plan_parser)
    _add_runtime_arguments(plan_parser)

    run_parser = subparsers.add_parser(
        "run",
        help="Ejecuta el loop completo de implementación, jueces y comprobaciones.",
    )
    _add_goal_arguments(run_parser)
    _add_runtime_arguments(run_parser)

    status_parser = subparsers.add_parser("status", help="Lee el último estado guardado.")
    status_parser.add_argument("--workspace", type=_workspace, default=Path.cwd())
    status_parser.add_argument("--config", type=Path)
    status_parser.add_argument("--run", type=Path, help="Directorio exacto de un run.")
    status_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            target = write_default_config(args.workspace, args.config, force=args.force)
            print(f"Configuración creada en {target}")
            return 0

        if args.command == "providers":
            config = load_config(args.workspace, args.config)
            rows = available_providers(config)
            if args.json:
                _print(rows, as_json=True)
            else:
                for row in rows:
                    mark = "OK" if row["installed"] else "no instalado"
                    print(f"{row['name']}: {mark} ({row['executable'] or 'configurable'})")
            return 0

        if args.command == "prompt":
            content = portable_prompt(_goal(args), args.bar, args.domain)
            if args.output:
                write_text(args.output.resolve(), content + "\n")
                print(f"Prompt guardado en {args.output.resolve()}")
            else:
                print(content)
            return 0

        if args.command == "status":
            config = load_config(args.workspace, args.config)
            run_path = args.run or _latest_run(
                args.workspace, config.get("artifacts_dir", ".manuloop/runs")
            )
            summary = load_run_summary(run_path)
            _print(summary if args.json else _summary_text(summary), as_json=args.json)
            return 0

        config = load_config(args.workspace, args.config)
        options = _options(args, config)
        runner = ManuLoopRunner(args.workspace, config, options)
        if args.command == "plan":
            summary = runner.run_plan(_goal(args), args.bar, args.domain)
            _print(summary if args.json else _summary_text(summary), as_json=args.json)
            return 0 if summary.get("status") == "PLAN_APPROVED" else 2
        if args.command == "run":
            summary = runner.run(_goal(args), args.bar, args.domain)
            _print(summary if args.json else _summary_text(summary), as_json=args.json)
            return 0 if summary.get("status", "").startswith("PASSED") else 2
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        print(f"manuloop: error: {exc}", file=sys.stderr)
        return 3
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
