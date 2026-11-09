import re
from pathlib import Path

from engine.health_check import HealthChecker
from engine.project_intelligence import ProjectScanner


class ErrorDiagnostics:
    """Read-only project diagnostics. Never applies repairs or executes shell commands."""

    def __init__(self, project_root, task_engine=None):
        self.root = Path(project_root).resolve()
        self.task_engine = task_engine

    @staticmethod
    def parse_traceback(text):
        if not isinstance(text, str) or not text.strip():
            return None

        text = text[-65536:]
        frames = []

        pattern = re.compile(
            r'File ["\']([^"\']+)["\'], line (\d+)(?:, in ([^\n]+))?'
        )

        for match in pattern.finditer(text):
            frames.append({
                "file": match.group(1)[:500],
                "line": int(match.group(2)),
                "function": (match.group(3) or "")[:200],
            })

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        exception = lines[-1][:500] if lines else ""

        return {
            "frames": frames[-30:],
            "exception": exception,
        }

    def diagnose(self, run_tests=False, test_name="core", traceback_text=None):
        scan = ProjectScanner(self.root).scan()
        health = HealthChecker(self.root).run()

        tests = None
        if run_tests:
            if self.task_engine is None:
                raise RuntimeError("Test execution requires a TaskEngine instance")
            try:
                tests = self.task_engine.run_registered_tests(test_name)
            except Exception as e:
                # Unexpected exception from test engine
                tests = {"success": False, "error": type(e).__name__}

        traceback = self.parse_traceback(traceback_text)

        syntax_errors = scan.get("errors", [])
        statuses = [health.get("status", "WARN")]

        if syntax_errors:
            statuses.append("FAIL")
        if tests is not None:
            statuses.append("PASS" if tests.get("success") else "FAIL")
        if traceback:
            statuses.append("WARN")

        status = (
            "FAIL" if "FAIL" in statuses
            else "WARN" if "WARN" in statuses
            else "PASS"
        )

        recommendations = []
        for error in syntax_errors:
            location = error.get("path", "unknown file")
            if error.get("line") is not None:
                location += f":{error['line']}"
            recommendations.append(
                f"Review Python syntax error at {location}."
            )

        if health.get("status") != "PASS":
            recommendations.append(
                "Review the failed or warning health checks before making changes."
            )

        if tests is not None and not tests.get("success"):
            recommendations.append(
                "Inspect the registered test output and identify the failing test."
            )

        if traceback:
            recommendations.append(
                "Use the traceback's final exception and frame locations to investigate the runtime error."
            )

        if not recommendations:
            recommendations.append(
                "No detected issue. This report does not prove that every runtime path works."
            )

        return {
            "status": status,
            "read_only": True,
            "automatic_repair": False,
            "ai_calls": False,
            "project_scan": {
                "summary": scan.get("summary", {}),
                "syntax_errors": syntax_errors,
                "dependencies": scan.get("dependencies", {}),
                "dependents": scan.get("dependents", {}),
                "skipped": scan.get("skipped", []),
            },
            "health": health,
            "tests": tests,
            "traceback": traceback,
            "recommendations": recommendations,
        }
