import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from engine.tool_registry import ToolRegistry
from security.policy import PolicyError, SecurityPolicy


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event, data):
        self.events.append((event, data))


class FakeEngine:
    def __init__(self, root):
        self.root = Path(root)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.policy = SecurityPolicy(self.root)
        self.audit = FakeAudit()
        self.run_registered_tests = Mock(
            return_value={
                "success": True,
                "stdout": "OK",
                "stderr": "",
            }
        )
        self.tasks = {}

    def get_task(self, task_id):
        return self.tasks.get(task_id)


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.temp.name)
        self.registry = ToolRegistry(self.engine)

        (self.engine.workspace / "sample.py").write_text(
            "value = 42\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_lists_workspace_files(self):
        result = self.registry.execute("list_workspace_files")
        self.assertIn("sample.py", result)

    def test_reads_workspace_file(self):
        result = self.registry.execute(
            "read_workspace_file", {"path": "sample.py"}
        )
        self.assertEqual(result, "value = 42\n")

    def test_rejects_path_traversal(self):
        with self.assertRaises(PolicyError):
            self.registry.execute(
                "read_workspace_file",
                {"path": "../outside.py"},
            )

    def test_rejects_unknown_tool(self):
        with self.assertRaises(PolicyError):
            self.registry.execute("run_shell", {})

    def test_rejects_extra_arguments(self):
        with self.assertRaises(PolicyError):
            self.registry.execute(
                "list_workspace_files", {"command": "whoami"}
            )

    def test_rejects_oversized_file(self):
        large = self.engine.workspace / "large.txt"
        large.write_bytes(b"x" * (ToolRegistry.MAX_READ_BYTES + 1))

        with self.assertRaises(PolicyError):
            self.registry.execute(
                "read_workspace_file", {"path": "large.txt"}
            )

    def test_runs_only_registered_test_suite(self):
        result = self.registry.execute(
            "run_registered_tests", {"test_name": "core"}
        )
        self.assertTrue(result["success"])
        self.engine.run_registered_tests.assert_called_once_with("core")

    def test_unregistered_test_is_rejected(self):
        with self.assertRaises(PolicyError):
            self.registry.execute(
                "run_registered_tests", {"test_name": "arbitrary"}
            )

    def test_build_project_context_for_existing_task(self):
        self.engine.tasks[7] = {
            "id": 7,
            "title": "Sample value engine",
            "description": "Inspect sample engine code",
        }

        result = self.registry.execute(
            "build_project_context", {"task_id": 7}
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], 7)
        self.assertIn("files", result)
        self.assertLessEqual(
            result["output_chars"],
            self.registry.MAX_CONTEXT_OUTPUT_CHARS,
        )

    def test_context_tool_rejects_missing_task(self):
        with self.assertRaises(PolicyError):
            self.registry.execute(
                "build_project_context", {"task_id": 999}
            )

    def test_context_tool_rejects_invalid_task_id(self):
        for invalid_id in ("7", True, 0, -1):
            with self.subTest(task_id=invalid_id):
                with self.assertRaises(PolicyError):
                    self.registry.execute(
                        "build_project_context",
                        {"task_id": invalid_id},
                    )

    def test_tool_calls_are_audited(self):
        self.registry.execute("list_workspace_files")
        event, data = self.engine.audit.events[-1]
        self.assertEqual(event, "TOOL_EXECUTED")
        self.assertEqual(data["tool"], "list_workspace_files")
        self.assertTrue(data["success"])

    def test_failed_calls_are_audited(self):
        with self.assertRaises(PolicyError):
            self.registry.execute("unknown", {})

        event, data = self.engine.audit.events[-1]
        self.assertEqual(event, "TOOL_EXECUTED")
        self.assertFalse(data["success"])


if __name__ == "__main__":
    unittest.main()
