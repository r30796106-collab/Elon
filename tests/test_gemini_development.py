import hashlib
import json
import unittest
from unittest.mock import patch

from providers.gemini_provider import GeminiProvider, ProviderError


class GeminiDevelopmentTests(unittest.TestCase):

    def setUp(self):
        self.provider = GeminiProvider()

        self.task = {
            "title": "Add calculator feature",
            "description": "Add a basic calculator module",
        }

        self.plan = {
            "steps": ["Create calculator function", "Add tests"]
        }

        self.test_result = {
            "success": True,
            "returncode": 0,
            "stdout": "Baseline tests passed",
            "stderr": "",
        }

        self.source_files = {
            "calculator.py": "def add(a, b):\n    return a + b\n"
        }

    @patch.object(GeminiProvider, "_generate")
    def test_development_proposal_uses_exact_file_hash(self, mock_generate):
        mock_generate.return_value = json.dumps({
            "action": "patch_set",
            "summary": "Added calculator feature",
            "patches": [{
                "path": "calculator.py",
                "expected_sha256": hashlib.sha256(
                    self.source_files["calculator.py"].encode("utf-8")
                ).hexdigest(),
                "content": "def add(a, b):\n    return a + b\n",
            }],
        })

        result = self.provider.propose_development_patch_set(
            self.task,
            self.plan,
            "core",
            self.test_result,
            self.source_files,
        )

        self.assertEqual(result["action"], "patch_set")
        self.assertEqual(len(result["patches"]), 1)

        prompt = json.loads(mock_generate.call_args.args[0])

        self.assertEqual(
            prompt["implementation_plan"], self.plan
        )
        self.assertEqual(
            prompt["source_files"]["calculator.py"]["sha256"],
            hashlib.sha256(
                self.source_files["calculator.py"].encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(
            prompt["source_files"]["calculator.py"]["content"],
            self.source_files["calculator.py"],
        )

    @patch.object(GeminiProvider, "_generate")
    def test_invalid_json_is_rejected(self, mock_generate):
        mock_generate.return_value = "not valid json"

        with self.assertRaises(ProviderError):
            self.provider.propose_development_patch_set(
                self.task,
                self.plan,
                "core",
                self.test_result,
                self.source_files,
            )

    @patch.object(GeminiProvider, "_generate")
    def test_non_object_json_is_rejected(self, mock_generate):
        mock_generate.return_value = json.dumps(["invalid"])

        with self.assertRaises(ProviderError):
            self.provider.propose_development_patch_set(
                self.task,
                self.plan,
                "core",
                self.test_result,
                self.source_files,
            )


    def tool_definitions(self):
        return {
            "list_workspace_files": set(),
            "read_workspace_file": {"path"},
            "run_registered_tests": {"test_name"},
        }

    @patch.object(GeminiProvider, "_generate")
    def test_valid_tool_call_is_returned(self, mock_generate):
        expected = {
            "action": "tool_call",
            "tool": "read_workspace_file",
            "arguments": {"path": "calculator.py"},
        }
        mock_generate.return_value = json.dumps(expected)

        result = self.provider.propose_tool_call(
            self.task, self.tool_definitions()
        )

        self.assertEqual(result, expected)
        prompt = json.loads(mock_generate.call_args.args[0])
        self.assertIn("read_workspace_file", prompt["available_tools"])

    @patch.object(GeminiProvider, "_generate")
    def test_invalid_tool_json_is_rejected(self, mock_generate):
        mock_generate.return_value = "not valid json"

        with self.assertRaises(ProviderError):
            self.provider.propose_tool_call(
                self.task, self.tool_definitions()
            )

    @patch.object(GeminiProvider, "_generate")
    def test_unknown_tool_is_rejected(self, mock_generate):
        mock_generate.return_value = json.dumps({
            "action": "tool_call",
            "tool": "run_shell",
            "arguments": {},
        })

        with self.assertRaises(ProviderError):
            self.provider.propose_tool_call(
                self.task, self.tool_definitions()
            )

    @patch.object(GeminiProvider, "_generate")
    def test_extra_tool_arguments_are_rejected(self, mock_generate):
        mock_generate.return_value = json.dumps({
            "action": "tool_call",
            "tool": "read_workspace_file",
            "arguments": {
                "path": "calculator.py",
                "command": "whoami",
            },
        })

        with self.assertRaises(ProviderError):
            self.provider.propose_tool_call(
                self.task, self.tool_definitions()
            )

    @patch.object(GeminiProvider, "_generate")
    def test_wrong_action_is_rejected(self, mock_generate):
        mock_generate.return_value = json.dumps({
            "action": "execute_shell",
            "tool": "list_workspace_files",
            "arguments": {},
        })

        with self.assertRaises(ProviderError):
            self.provider.propose_tool_call(
                self.task, self.tool_definitions()
            )

    @patch.object(GeminiProvider, "_generate")
    def test_no_tool_response_is_supported(self, mock_generate):
        expected = {
            "action": "no_tool",
            "reason": "No file inspection is needed.",
        }
        mock_generate.return_value = json.dumps(expected)

        result = self.provider.propose_tool_call(
            self.task, self.tool_definitions()
        )

        self.assertEqual(result, expected)


    @patch.object(GeminiProvider, "_generate")
    def test_valid_task_plan_is_returned(self, mock_generate):
        expected = {
            "action": "plan",
            "objective": "Add calculator feature",
            "requirements": ["Support addition"],
            "steps": [{
                "id": 1,
                "description": "Inspect calculator module",
                "type": "inspect",
                "files": ["workspace/calculator.py"],
                "tests": []
            }],
            "risks": [],
            "requires_approval": False
        }
        mock_generate.return_value = json.dumps(expected)

        context = {
            "files": [{"path": "workspace/calculator.py"}]
        }

        result = self.provider.propose_task_plan(self.task, context)

        self.assertEqual(result, expected)
        mock_generate.assert_called_once()

    @patch.object(GeminiProvider, "_generate")
    def test_invalid_task_plan_json_is_rejected(self, mock_generate):
        mock_generate.return_value = "not valid json"

        with self.assertRaises(ProviderError):
            self.provider.propose_task_plan(self.task, {"files": []})

    @patch.object(GeminiProvider, "_generate")
    def test_non_object_task_plan_is_rejected(self, mock_generate):
        mock_generate.return_value = json.dumps(["invalid"])

        with self.assertRaises(ProviderError):
            self.provider.propose_task_plan(self.task, {"files": []})

    @patch.object(GeminiProvider, "_generate")
    def test_task_plan_prompt_contains_safety_constraints(self, mock_generate):
        mock_generate.return_value = json.dumps({"action": "plan"})

        self.provider.propose_task_plan(
            self.task,
            {"files": [{"path": "workspace/calculator.py"}]}
        )

        prompt = json.loads(mock_generate.call_args.args[0])
        instructions = mock_generate.call_args.args[1]

        self.assertEqual(prompt["registered_test_suites"], ["core", "demo"])
        self.assertIn("workspace/calculator.py", prompt["project_context"]["files"][0]["path"])
        self.assertIn("Do not execute tools", instructions)
        self.assertIn("Never plan shell access", instructions)
        self.assertEqual(mock_generate.call_args.kwargs["max_tokens"], 6000)


if __name__ == "__main__":
    unittest.main()
