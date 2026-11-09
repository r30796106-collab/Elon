import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine.error_diagnostics import ErrorDiagnostics


class ErrorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        for folder in (
            "core", "engine", "providers", "security", "tests", "workspace"
        ):
            (self.root / folder).mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_syntax_error_is_reported(self):
        (self.root / "core" / "broken.py").write_text(
            "def broken(:\n", encoding="utf-8"
        )

        with patch(
            "engine.error_diagnostics.HealthChecker.run",
            return_value={"status": "PASS", "checks": {}, "read_only": True},
        ):
            report = ErrorDiagnostics(self.root).diagnose()

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["automatic_repair"])
        self.assertEqual(
            report["project_scan"]["syntax_errors"][0]["path"],
            "core/broken.py",
        )

    def test_valid_project_has_no_syntax_errors(self):
        (self.root / "core" / "valid.py").write_text(
            "VALUE = 42\n", encoding="utf-8"
        )

        with patch(
            "engine.error_diagnostics.HealthChecker.run",
            return_value={"status": "PASS", "checks": {}, "read_only": True},
        ):
            report = ErrorDiagnostics(self.root).diagnose()

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["project_scan"]["syntax_errors"], [])

    def test_tests_are_not_run_by_default(self):
        engine = Mock()

        with patch(
            "engine.error_diagnostics.HealthChecker.run",
            return_value={"status": "PASS", "checks": {}, "read_only": True},
        ):
            report = ErrorDiagnostics(self.root, engine).diagnose()

        engine.run_registered_tests.assert_not_called()
        self.assertIsNone(report["tests"])

    def test_explicit_tests_use_registered_test_runner(self):
        engine = Mock()
        engine.run_registered_tests.return_value = {
            "success": True, "returncode": 0, "stdout": "OK", "stderr": ""
        }

        with patch(
            "engine.error_diagnostics.HealthChecker.run",
            return_value={"status": "PASS", "checks": {}, "read_only": True},
        ):
            report = ErrorDiagnostics(self.root, engine).diagnose(
                run_tests=True, test_name="core"
            )

        engine.run_registered_tests.assert_called_once_with("core")
        self.assertTrue(report["tests"]["success"])

    def test_traceback_parser_extracts_frame_and_exception(self):
        text = (
            'Traceback (most recent call last):\n'
            '  File "/tmp/example.py", line 27, in run\n'
            'ValueError: invalid input\n'
        )

        result = ErrorDiagnostics.parse_traceback(text)

        self.assertEqual(result["frames"][0]["file"], "/tmp/example.py")
        self.assertEqual(result["frames"][0]["line"], 27)
        self.assertEqual(result["exception"], "ValueError: invalid input")

    def test_empty_traceback_returns_none(self):
        self.assertIsNone(ErrorDiagnostics.parse_traceback("  "))

    def test_diagnose_handles_task_engine_exception(self):
        engine = Mock()
        engine.run_registered_tests.side_effect = RuntimeError("unexpected")
        with patch(
            "engine.error_diagnostics.HealthChecker.run",
            return_value={"status": "PASS", "checks": {}, "read_only": True},
        ):
            report = ErrorDiagnostics(self.root, engine).diagnose(run_tests=True, test_name="core")
        self.assertEqual(report["status"], "FAIL")
        self.assertIsNotNone(report["tests"])
        self.assertFalse(report["tests"]["success"])
        self.assertEqual(report["tests"]["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
