import tempfile
import unittest
from pathlib import Path

from engine.ai_tool_controller import AIToolController
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

    def run_registered_tests(self, test_name):
        return {"success": True, "stdout": "OK", "stderr": ""}


class AIToolControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.temp.name)
        (self.engine.workspace / "sample.py").write_text(
            "value = 42\n", encoding="utf-8"
        )
        self.registry = ToolRegistry(self.engine)
        self.controller = AIToolController(self.registry)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, tool="list_workspace_files", arguments=None):
        if arguments is None:
            arguments = {}
        return {
            "action": "tool_call",
            "tool": tool,
            "arguments": arguments,
        }

    def test_executes_registered_tool(self):
        result = self.controller.handle_request(self.request())
        self.assertTrue(result["success"])
        self.assertIn("sample.py", result["result"])

    def test_rejects_non_object_request(self):
        with self.assertRaises(PolicyError):
            self.controller.handle_request("tool_call")

    def test_rejects_extra_request_fields(self):
        request = self.request()
        request["shell"] = "whoami"
        with self.assertRaises(PolicyError):
            self.controller.handle_request(request)

    def test_rejects_wrong_action(self):
        request = self.request()
        request["action"] = "execute_shell"
        with self.assertRaises(PolicyError):
            self.controller.handle_request(request)

    def test_rejects_unknown_tool(self):
        with self.assertRaises(PolicyError):
            self.controller.handle_request(self.request("run_shell"))

    def test_rejects_extra_tool_arguments(self):
        with self.assertRaises(PolicyError):
            self.controller.handle_request(
                self.request("read_workspace_file", {
                    "path": "sample.py",
                    "command": "whoami",
                })
            )

    def test_enforces_five_calls_per_cycle(self):
        for _ in range(5):
            self.controller.handle_request(self.request())

        with self.assertRaises(PolicyError):
            self.controller.handle_request(self.request())

        self.assertEqual(self.controller.calls_used, 5)

    def test_new_cycle_resets_call_budget(self):
        for _ in range(5):
            self.controller.handle_request(self.request())

        self.controller.begin_cycle()
        result = self.controller.handle_request(self.request())

        self.assertTrue(result["success"])
        self.assertEqual(self.controller.calls_used, 1)

    def test_truncates_large_tool_output(self):
        (self.engine.workspace / "large.txt").write_text(
            "A" * 20000, encoding="utf-8"
        )
        result = self.controller.handle_request(
            self.request("read_workspace_file", {"path": "large.txt"})
        )
        self.assertTrue(result["result"]["truncated"])
        self.assertLessEqual(
            len(result["result"]["preview"]),
            self.controller.MAX_OUTPUT_CHARS,
        )

    def test_rejected_request_is_audited(self):
        with self.assertRaises(PolicyError):
            self.controller.handle_request(self.request("run_shell"))

        event, data = self.engine.audit.events[-1]
        self.assertEqual(event, "AI_TOOL_REQUEST_REJECTED")
        self.assertIn("reason", data)

    def test_registry_audits_successful_execution(self):
        self.controller.handle_request(self.request())
        self.assertTrue(any(
            event == "TOOL_EXECUTED" and data["success"]
            for event, data in self.engine.audit.events
        ))


if __name__ == "__main__":
    unittest.main()
