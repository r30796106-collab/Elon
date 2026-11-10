import tempfile
import unittest
from pathlib import Path

from engine.project_intelligence import ProjectScanner


class ProjectScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        for folder in ("core", "engine", "providers", "tests", "workspace"):
            (self.root / folder).mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_scans_python_modules(self):
        self.write("core/__init__.py", "")
        self.write("core/helper.py", "VALUE = 42\n")

        result = ProjectScanner(self.root).scan()

        self.assertTrue(result["success"])
        self.assertIn("core", result["modules"])
        self.assertIn("core.helper", result["modules"])

    def test_maps_internal_imports(self):
        self.write("core/helper.py", "VALUE = 42\n")
        self.write("engine/worker.py", "from core.helper import VALUE\n")

        result = ProjectScanner(self.root).scan()

        self.assertEqual(
            result["dependencies"]["engine.worker"],
            ["core.helper"],
        )

    def test_external_imports_are_ignored(self):
        self.write("core/helper.py", "import json\nimport os\n")

        result = ProjectScanner(self.root).scan()

        self.assertEqual(result["dependencies"]["core.helper"], [])

    def test_direct_and_transitive_impact(self):
        self.write("core/base.py", "VALUE = 1\n")
        self.write("engine/middle.py", "from core.base import VALUE\n")
        self.write("providers/top.py", "from engine.middle import VALUE\n")

        scanner = ProjectScanner(self.root)
        result = scanner.scan()
        impact = scanner.analyze_impact("core.base", result)

        self.assertEqual(
            impact["direct_dependents"], ["engine.middle"]
        )
        self.assertEqual(
            impact["transitive_dependents"],
            ["providers.top"],
        )

    def test_unknown_module_is_rejected(self):
        scanner = ProjectScanner(self.root)

        with self.assertRaises(ValueError):
            scanner.analyze_impact("unknown.module")

    def test_oversized_file_is_skipped(self):
        path = self.write("core/large.py", "x" * (
            ProjectScanner.MAX_FILE_BYTES + 1
        ))

        result = ProjectScanner(self.root).scan()

        self.assertNotIn("core.large", result["modules"])
        self.assertIn("core/large.py", result["skipped"])

    def test_syntax_error_is_reported(self):
        self.write("core/broken.py", "def broken(:\n")

        result = ProjectScanner(self.root).scan()

        self.assertEqual(result["summary"]["syntax_errors"], 1)
        self.assertTrue(result["errors"])

    def test_syntax_error_includes_location_and_message(self):
        self.write("core/broken.py", "def broken(:\\n")

        result = ProjectScanner(self.root).scan()
        error = result["errors"][0]

        self.assertEqual(error["path"], "core/broken.py")
        self.assertEqual(error["error"], "SyntaxError")
        self.assertEqual(error["line"], 1)
        self.assertIsNotNone(error["column"])
        self.assertTrue(error["message"])

    def test_scanner_does_not_execute_code(self):
        marker = self.root / "executed.txt"
        self.write(
            "core/unsafe.py",
            f"from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n",
        )

        ProjectScanner(self.root).scan()

        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
