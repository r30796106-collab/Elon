import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.environment_inspector import EnvironmentInspector


class EnvironmentInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inspector = EnvironmentInspector(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_reports_runtime_without_installing_or_network_access(self):
        report = self.inspector.inspect()

        self.assertIn(report["status"], ("PASS", "WARN"))
        self.assertTrue(report["read_only"])
        self.assertFalse(report["network_used"])
        self.assertFalse(report["installation_performed"])
        self.assertIn("BLOCKED", report["package_installation"])
        self.assertTrue(report["python"]["version"])

    def test_detects_missing_python_and_node_dependencies(self):
        (self.root / "requirements.txt").write_text(
            "elon-definitely-missing-package-xyz>=1.0\n",
            encoding="utf-8",
        )
        (self.root / "package.json").write_text(
            json.dumps({
                "dependencies": {"elon-definitely-missing-node-package": "^1.0.0"}
            }),
            encoding="utf-8",
        )

        report = self.inspector.inspect()

        self.assertEqual(report["status"], "WARN")
        self.assertIn(
            "elon-definitely-missing-package-xyz",
            report["python_dependencies"]["missing"],
        )
        self.assertIn(
            "elon-definitely-missing-node-package",
            report["node_dependencies"]["missing"],
        )

    def test_detects_installed_python_dependency_without_importing_it(self):
        (self.root / "requirements.txt").write_text(
            "sample-installed-dist>=1.0\n",
            encoding="utf-8",
        )

        with patch(
            "engine.environment_inspector.importlib.metadata.version",
            return_value="1.2.3",
        ) as version:
            report = self.inspector.inspect()

        version.assert_called_once_with("sample-installed-dist")
        self.assertEqual(
            report["python_dependencies"]["installed"],
            [{"name": "sample-installed-dist", "version": "1.2.3"}],
        )

    def test_invalid_package_json_is_reported_as_warning(self):
        (self.root / "package.json").write_text(
            "{ invalid json",
            encoding="utf-8",
        )

        report = self.inspector.inspect()

        self.assertEqual(report["status"], "WARN")
        self.assertIn(
            "package.json is invalid JSON.",
            report["warnings"],
        )

    def test_symlinked_manifest_is_not_read(self):
        outside = Path(self.temp.name).parent / (
            Path(self.temp.name).name + "_requirements.txt"
        )
        outside.write_text("secret-package-name\n", encoding="utf-8")
        try:
            (self.root / "requirements.txt").symlink_to(outside)
            report = self.inspector.inspect()
            self.assertEqual(report["python_dependencies"]["declared"], [])
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
