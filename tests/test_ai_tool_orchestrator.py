import unittest

from engine.ai_tool_orchestrator import AIToolOrchestrator


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event, details):
        self.events.append((event, details))


class FakeEngine:
    def __init__(self):
        self.audit = FakeAudit()


class FakeRegistry:
    def __init__(self):
        self.tools = {
            "list_workspace_files": set(),
            "read_workspace_file": {"path"},
        }
        self.engine = FakeEngine()


class FakeController:
    def __init__(self):
        self.registry = FakeRegistry()
        self.calls = []
        self.begin_count = 0

    def begin_cycle(self):
        self.begin_count += 1
        self.calls = []

    def handle_request(self, request):
        self.calls.append(request)
        return {
            "success": True,
            "tool": request["tool"],
            "result": {"output": "result-" + str(len(self.calls))},
        }


class FakeProvider:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.contexts = []

    def propose_tool_call(self, task, available_tools):
        self.contexts.append(task)
        if not self.proposals:
            raise AssertionError("Unexpected extra provider call")
        return self.proposals.pop(0)


def tool_call(name="list_workspace_files", arguments=None):
    if arguments is None:
        arguments = {}
    return {
        "action": "tool_call",
        "tool": name,
        "arguments": arguments,
    }


class AIToolOrchestratorTests(unittest.TestCase):

    def make_orchestrator(self, proposals):
        provider = FakeProvider(proposals)
        controller = FakeController()
        orchestrator = AIToolOrchestrator(provider, controller)
        return orchestrator, provider, controller

    def test_no_tool_stops_without_execution(self):
        orchestrator, provider, controller = self.make_orchestrator([
            {"action": "no_tool", "reason": "No tool needed"}
        ])

        result = orchestrator.run_cycle("Explain this code")

        self.assertEqual(result["status"], "no_tool")
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(controller.calls, [])
        self.assertEqual(controller.begin_count, 1)

    def test_one_tool_call(self):
        orchestrator, provider, controller = self.make_orchestrator([
            tool_call(),
            {"action": "no_tool", "reason": "Finished"},
        ])

        result = orchestrator.run_cycle("Inspect workspace")

        self.assertEqual(result["status"], "no_tool")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len(controller.calls), 1)

    def test_tool_result_is_passed_to_next_provider_request(self):
        orchestrator, provider, controller = self.make_orchestrator([
            tool_call(),
            {"action": "no_tool", "reason": "Finished"},
        ])

        orchestrator.run_cycle("Inspect workspace")

        self.assertEqual(len(provider.contexts), 2)
        history = provider.contexts[1]["tool_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["tool"], "list_workspace_files")
        self.assertEqual(history[0]["result"]["output"], "result-1")

    def test_stops_after_five_tool_calls(self):
        proposals = [
            tool_call("read_workspace_file", {"path": f"file_{i}.py"})
            for i in range(5)
        ]
        orchestrator, provider, controller = self.make_orchestrator(proposals)

        result = orchestrator.run_cycle("Inspect workspace")

        self.assertEqual(result["status"], "max_calls_reached")
        self.assertEqual(result["tool_calls"], 5)
        self.assertEqual(len(controller.calls), 5)
        self.assertEqual(len(provider.contexts), 5)

    def test_invalid_task_rejected(self):
        orchestrator, provider, controller = self.make_orchestrator([])

        with self.assertRaises(ValueError):
            orchestrator.run_cycle(None)

        self.assertEqual(controller.begin_count, 0)

    def test_invalid_no_tool_proposal_rejected(self):
        orchestrator, provider, controller = self.make_orchestrator([
            {"action": "no_tool", "reason": "Done", "extra": "bad"}
        ])

        with self.assertRaises(ValueError):
            orchestrator.run_cycle("Test")

        self.assertEqual(controller.calls, [])

    def test_unsupported_action_rejected(self):
        orchestrator, provider, controller = self.make_orchestrator([
            {"action": "delete_everything"}
        ])

        with self.assertRaises(ValueError):
            orchestrator.run_cycle("Test")

        self.assertEqual(controller.calls, [])

    def test_new_cycle_resets_controller(self):
        orchestrator, provider, controller = self.make_orchestrator([
            {"action": "no_tool", "reason": "First"},
            {"action": "no_tool", "reason": "Second"},
        ])

        orchestrator.run_cycle("First task")
        orchestrator.run_cycle("Second task")

        self.assertEqual(controller.begin_count, 2)

    def test_duplicate_identical_request_is_blocked_and_audited(self):
        repeated = tool_call()
        orchestrator, provider, controller = self.make_orchestrator([
            repeated,
            repeated,
        ])

        result = orchestrator.run_cycle("Inspect workspace")

        self.assertEqual(result["status"], "duplicate_call_blocked")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len(controller.calls), 1)
        self.assertEqual(result["blocked_tool"], "list_workspace_files")

        events = controller.registry.engine.audit.events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "AI_TOOL_DUPLICATE_BLOCKED")

    def test_same_tool_with_different_arguments_is_allowed(self):
        orchestrator, provider, controller = self.make_orchestrator([
            tool_call("read_workspace_file", {"path": "one.py"}),
            tool_call("read_workspace_file", {"path": "two.py"}),
            {"action": "no_tool", "reason": "Finished"},
        ])

        result = orchestrator.run_cycle("Inspect two files")

        self.assertEqual(result["status"], "no_tool")
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(len(controller.calls), 2)


if __name__ == "__main__":
    unittest.main()
