import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.performance_profiler import PerformanceProfiler


class PerformanceProfilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_invalid_iteration_counts(self):
        profiler = PerformanceProfiler(self.root)
        for value in (0, 11, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    profiler.benchmark(value)

    def test_runs_bounded_read_only_benchmarks(self):
        profiler = PerformanceProfiler(self.root)
        with patch(
            "engine.performance_profiler.ProjectScanner.scan",
            return_value={"summary": {"modules_scanned": 4}},
        ) as python_scan, patch(
            "engine.performance_profiler.JavaScriptScanner.scan",
            return_value={"status": "PASS", "files_scanned": 2},
        ) as js_scan, patch(
            "engine.performance_profiler.HealthChecker.run",
            return_value={"status": "PASS"},
        ) as health, patch(
            "engine.performance_profiler.ProjectContextBuilder.build",
            return_value={"success": True, "summary": {"files_selected": 3}},
        ) as context:
            report = profiler.benchmark(iterations=2)

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["optimization_applied"])
        self.assertEqual(report["iterations"], 2)
        self.assertEqual(len(report["benchmarks"]), 4)
        for result in report["benchmarks"].values():
            self.assertEqual(result["runs"], 2)
            self.assertIsNotNone(result["median_seconds"])
        self.assertEqual(python_scan.call_count, 2)
        self.assertEqual(js_scan.call_count, 2)
        self.assertEqual(health.call_count, 2)
        self.assertEqual(context.call_count, 2)

    def test_reports_operation_errors_without_claiming_success(self):
        profiler = PerformanceProfiler(self.root)
        with patch(
            "engine.performance_profiler.ProjectScanner.scan",
            side_effect=OSError("simulated scan failure"),
        ):
            report = profiler.benchmark(iterations=1)

        self.assertEqual(report["status"], "WARN")
        self.assertEqual(
            report["errors"][0],
            {
                "operation": "python_project_scan",
                "error": "OSError",
            },
        )
        self.assertEqual(
            report["benchmarks"]["python_project_scan"]["runs"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
