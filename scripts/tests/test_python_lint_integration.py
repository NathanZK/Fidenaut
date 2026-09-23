import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]


class PythonLintIntegrationTest(unittest.TestCase):
    def test_lint_entrypoint_executes_ruff(self):
        entrypoint = ROOT / "scripts" / "run_python_lint.py"
        self.assertTrue(entrypoint.is_file())
        completed = subprocess.run(
            [sys.executable, str(entrypoint)],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "lint entrypoint failed:\n%s%s"
            % (completed.stdout, completed.stderr),
        )

    def test_ci_installs_pinned_lint_and_preserves_provider_tests(self):
        requirements = (ROOT / "requirements-lint.txt").read_text(encoding="utf-8")
        self.assertRegex(requirements, r"(?m)^ruff==[0-9]+\.[0-9]+\.[0-9]+$")

        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 -m pip install -r requirements-lint.txt", workflow)
        self.assertIn("python3 scripts/run_python_lint.py", workflow)
        self.assertIn("python3 scripts/run_agent_workflow_tests.py", workflow)


if __name__ == "__main__":
    unittest.main()
