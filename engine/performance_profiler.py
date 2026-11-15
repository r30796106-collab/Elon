"""Read-only performance baselines for ELON project inspection workflows."""

import statistics
import time
from pathlib import Path

from engine.health_check import HealthChecker
from engine.javascript_intelligence import JavaScriptScanner
from engine.project_context import ProjectContextBuilder
from engine.project_intelligence import ProjectScanner


class PerformanceProfiler:
    MAX_ITERATIONS = 10

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()

    def benchmark(self, iterations=3):
        if isinstance(iterations, bool) or not isinstance(iterations, int):
            raise ValueError("Iterations must be an integer.")
        if not 1 <= iterations <= self.MAX_ITERATIONS:
            raise ValueError(
                f"Iterations must be between 1 and {self.MAX_ITERATIONS}."
            )

        context_builder = ProjectContextBuilder(self.root)
        operations = {
            "python_project_scan": lambda: ProjectScanner(self.root).scan(),
            "javascript_project_scan": lambda: JavaScriptScanner(self.root).scan(),
            "health_check": lambda: HealthChecker(self.root).run(),
            "project_context_build": lambda: context_builder.build({
                "title": "Project performance inspection",
                "description": "Inspect project scanner and diagnostics performance.",
            }),
        }

        results = {}
        errors = []

        for name, operation in operations.items():
            durations = []
            last_result = None

            for _ in range(iterations):
                started = time.perf_counter()
                try:
                    last_result = operation()
                except Exception as error:
                    errors.append({
                        "operation": name,
                        "error": type(error).__name__,
                    })
                    break
                durations.append(time.perf_counter() - started)

            if durations:
                results[name] = {
                    "runs": len(durations),
                    "median_seconds": round(
                        statistics.median(durations), 6
                    ),
                    "min_seconds": round(min(durations), 6),
                    "max_seconds": round(max(durations), 6),
                }
            else:
                results[name] = {
                    "runs": 0,
                    "median_seconds": None,
                    "min_seconds": None,
                    "max_seconds": None,
                }

            if isinstance(last_result, dict):
                summary = last_result.get("summary")
                if isinstance(summary, dict):
                    results[name]["summary"] = summary
                status = last_result.get("status")
                if isinstance(status, str):
                    results[name]["status"] = status

        return {
            "status": "WARN" if errors else "PASS",
            "read_only": True,
            "optimization_applied": False,
            "iterations": iterations,
            "benchmarks": results,
            "errors": errors,
        }
