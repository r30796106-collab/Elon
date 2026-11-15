import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.javascript_intelligence import JavaScriptScanner


class JavaScriptScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "src"
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def require_node(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

    def test_scans_modules_and_local_import_dependencies(self):
        self.require_node()
        (self.source / "util.mjs").write_text(
            "export const value = 7;\n",
            encoding="utf-8",
        )
        (self.source / "main.mjs").write_text(
            'import { value } from "./util.mjs";\n'
            "export const result = value;\n",
            encoding="utf-8",
        )

        report = JavaScriptScanner(self.root).scan()

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["files_scanned"], 2)
        self.assertEqual(
            report["dependencies"]["src/main"],
            ["src/util"],
        )
        self.assertTrue(report["read_only"])
        self.assertFalse(report["executed_source"])

    def test_reports_javascript_syntax_errors(self):
        self.require_node()
        (self.source / "broken.mjs").write_text(
            "export const = ;\n",
            encoding="utf-8",
        )

        report = JavaScriptScanner(self.root).scan()

        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["syntax_errors"]), 1)
        self.assertEqual(
            report["syntax_errors"][0]["path"],
            "src/broken.mjs",
        )
        self.assertEqual(
            report["syntax_errors"][0]["language"],
            "javascript",
        )

    def test_syntax_check_does_not_execute_source(self):
        self.require_node()
        marker = self.root / "SHOULD_NOT_EXIST"
        (self.source / "side_effect.mjs").write_text(
            'import fs from "node:fs";\n'
            f'fs.writeFileSync({str(marker)!r}, "executed");\n'
            'throw new Error("This file must not execute during scanning");\n',
            encoding="utf-8",
        )

        report = JavaScriptScanner(self.root).scan()

        self.assertEqual(report["status"], "PASS")
        self.assertFalse(marker.exists())
        self.assertFalse(report["executed_source"])

    def test_total_syntax_check_budget_is_bounded(self):
        self.require_node()
        (self.source / "one.mjs").write_text(
            "export const one = 1;\\n",
            encoding="utf-8",
        )
        (self.source / "two.mjs").write_text(
            "export const two = 2;\\n",
            encoding="utf-8",
        )

        with patch.object(JavaScriptScanner, "MAX_TOTAL_CHECK_SECONDS", 0):
            report = JavaScriptScanner(self.root).scan()

        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["files_scanned"], 2)
        self.assertIn("src/one", report["modules"])
        self.assertIn("src/two", report["modules"])
        self.assertTrue(report["warnings"])
        self.assertIn(
            "JavaScript syntax-check time budget reached",
            report["skipped"],
        )

    def test_missing_node_warns_when_javascript_files_exist(self):
        (self.source / "main.mjs").write_text(
            "export const value = 1;\n",
            encoding="utf-8",
        )

        with patch(
            "engine.javascript_intelligence.shutil.which",
            return_value=None,
        ):
            report = JavaScriptScanner(self.root).scan()

        self.assertEqual(report["status"], "WARN")
        self.assertFalse(report["node_available"])
        self.assertEqual(report["files_scanned"], 1)
        self.assertIn("src/main", report["modules"])

    def test_symlinked_javascript_file_is_skipped(self):
        outside = self.root.parent / (self.root.name + "_outside.mjs")
        outside.write_text("export const secret = 1;\n", encoding="utf-8")
        try:
            (self.source / "linked.mjs").symlink_to(outside)
            report = JavaScriptScanner(self.root).scan()
            self.assertIn("src/linked.mjs", report["skipped"])
            self.assertNotIn("src/linked", report["modules"])
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
