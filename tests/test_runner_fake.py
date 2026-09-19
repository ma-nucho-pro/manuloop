import json
import sys
import tempfile
import unittest
from pathlib import Path

from manuloop.runner import ManuLoopRunner, RunnerOptions


FAKE_AGENT = r'''
import json
import pathlib
import sys

prompt = sys.argv[-1] if len(sys.argv) > 1 else sys.stdin.read()

def emit(value):
    print("MANULOOP_JSON_BEGIN")
    print(json.dumps(value))
    print("MANULOOP_JSON_END")

if "ROL: líder de planificación" in prompt:
    emit({
        "quality_bar": "El archivo generado existe y los checks explícitos pasan.",
        "workstreams": [{
            "id": "core",
            "title": "Implementación central",
            "objective": "Crear el artefacto mínimo verificable.",
            "owned_paths": ["fake-built.txt"],
            "done_when": ["fake-built.txt existe"],
            "verification": ["check explícito"],
            "dependencies": []
        }],
        "verification_commands": [],
        "risks": [],
        "assumptions": []
    })
elif "ROL: juez independiente" in prompt or "ROL: juez final independiente" in prompt:
    emit({
        "verdict": "PASS",
        "confidence": 0.99,
        "checks": [{"name": "fake", "status": "PASS", "evidence": "ok"}],
        "critical_findings": [],
        "next_action": "continuar"
    })
elif "ROL: builder de implementación" in prompt or "ROL: integrador" in prompt or "ROL: fixer" in prompt:
    pathlib.Path("fake-built.txt").write_text("verified\n", encoding="utf-8")
    print("Se escribió el artefacto y se revisó el diff.")
else:
    print("Exploración independiente completada.")
'''


def fake_config(tmp_path: Path, fake_path: Path) -> dict:
    command = [sys.executable, str(fake_path), "{prompt}"]
    return {
        "providers": {
            "custom": {
                "command": command,
                "readonly_command": command,
                "output": "text",
            }
        }
    }


class RunnerTests(unittest.TestCase):
    def test_runner_completes_with_fresh_fake_judges(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            fake_path = tmp_path / "fake_agent.py"
            fake_path.write_text(FAKE_AGENT, encoding="utf-8")
            check = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'fake-built.txt\').exists() else 1)"'
            runner = ManuLoopRunner(
                tmp_path,
                fake_config(tmp_path, fake_path),
                RunnerOptions(
                    provider="custom",
                    judge_provider="custom",
                    scout_count=2,
                    judge_count=2,
                    max_rounds=1,
                    preflight_rounds=1,
                    timeout_seconds=30,
                    allow_edits=True,
                    checks=[check],
                    artifacts_dir=str(tmp_path / "receipts"),
                ),
            )
            summary = runner.run(
                "Crea el artefacto de prueba",
                "El artefacto existe y el check pasa",
                "code",
            )

            self.assertEqual(summary["status"], "PASSED")
            self.assertTrue((tmp_path / "fake-built.txt").exists())
            state = json.loads(
                (Path(summary["artifact_dir"]) / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "PASSED")
            self.assertTrue((Path(summary["artifact_dir"]) / "events.jsonl").exists())

    def test_runner_blocks_without_edits_before_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            fake_path = tmp_path / "fake_agent.py"
            fake_path.write_text(FAKE_AGENT, encoding="utf-8")
            runner = ManuLoopRunner(
                tmp_path,
                fake_config(tmp_path, fake_path),
                RunnerOptions(
                    provider="custom",
                    judge_provider="custom",
                    scout_count=1,
                    judge_count=1,
                    max_rounds=1,
                    preflight_rounds=1,
                    timeout_seconds=30,
                    allow_edits=False,
                    allow_no_checks=True,
                    artifacts_dir=str(tmp_path / "receipts"),
                ),
            )
            summary = runner.run("Crea el artefacto", "debe existir", "code")
            self.assertEqual(summary["status"], "PLAN_APPROVED")
            self.assertFalse((tmp_path / "fake-built.txt").exists())

    def test_runner_does_not_trust_judge_when_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            fake_path = tmp_path / "fake_agent.py"
            fake_path.write_text(FAKE_AGENT, encoding="utf-8")
            bad_check = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
            runner = ManuLoopRunner(
                tmp_path,
                fake_config(tmp_path, fake_path),
                RunnerOptions(
                    provider="custom",
                    judge_provider="custom",
                    scout_count=1,
                    judge_count=1,
                    max_rounds=1,
                    preflight_rounds=1,
                    timeout_seconds=30,
                    allow_edits=True,
                    checks=[bad_check],
                    artifacts_dir=str(tmp_path / "receipts"),
                ),
            )
            summary = runner.run("Crea el artefacto", "debe existir", "code")
            self.assertEqual(summary["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
