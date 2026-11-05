import unittest
from pathlib import Path
from unittest.mock import patch

from engine.autonomous_controller import AutonomousController


class FakeAudit:
    def append(self, *args, **kwargs):
        pass


class FakePolicy:
    MAX_REPAIRS = 1


class FakeEngine:
    def __init__(self, test_results=None, completion_error=False):
        self.task = {
            "id": 1,
            "title": "Test development",
            "description": "Mock task",
            "status": "pending",
            "repair_attempts": 0,
        }
        self.policy = FakePolicy()
        self.audit = FakeAudit()
        self.test_results = list(test_results or [
            {"success": True},
            {"success": True},
        ])
        self.completion_error = completion_error
        self.completed = False
        self.test_names = []

    def get_task(self, task_id):
        return self.task if task_id == 1 else None

    def run_registered_tests(self, test_name):
        self.test_names.append(test_name)
        return self.test_results.pop(0)

    def complete_task(self, task_id):
        if self.completion_error:
            raise RuntimeError("Mock completion failure")
        self.completed = True
        self.task["status"] = "completed"


class FakeProvider:
    model = "mock-model"

    def __init__(self, proposal=None):
        self.proposal = proposal or {
            "action": "patch_set",
            "summary": "Mock feature",
            "patches": [{
                "path": "ELON/workspace/app.py",
                "content": "value = 2\n",
                "expected_sha256": "mock-hash",
            }],
        }

    def configured(self):
        return True

    def propose_development_patch_set(self, *args):
        return self.proposal


class FakeRepairEngine:
    def _collect_context(self):
        return {}


class FakeTransaction:
    def __init__(self):
        self.applied = False
        self.rolled_back = False

    def apply(self, task_id, patches):
        self.applied = True
        return [{"path": patches[0]["path"]}]

    def rollback(self, applied):
        self.rolled_back = True
        return []


class AutonomousControllerTests(unittest.TestCase):

    def make_controller(self, engine, provider=None):
        controller = AutonomousController.__new__(AutonomousController)
        controller.root = Path.home() / "ELON"
        controller.repair_engine = FakeRepairEngine()
        controller.provider = provider or FakeProvider()
        controller._load_plan = lambda engine, task_id: "Mock plan"
        controller._collect_safe_context = lambda engine, plan: {
            "ELON/workspace/app.py": "value = 1\n"
        }
        return controller

    def test_success_keeps_patch_and_completes_task(self):
        engine = FakeEngine()
        controller = self.make_controller(engine)
        transaction = FakeTransaction()

        with patch(
            "engine.autonomous_controller.PatchTransaction",
            return_value=transaction,
        ):
            result = controller.develop_task(engine, 1)

        self.assertTrue(result["success"])
        self.assertTrue(engine.completed)
        self.assertTrue(transaction.applied)
        self.assertFalse(transaction.rolled_back)

    def test_failed_tests_trigger_rollback(self):
        engine = FakeEngine([
            {"success": True},
            {"success": False},
        ])
        controller = self.make_controller(engine)
        transaction = FakeTransaction()

        with patch(
            "engine.autonomous_controller.PatchTransaction",
            return_value=transaction,
        ):
            result = controller.develop_task(engine, 1)

        self.assertFalse(result["success"])
        self.assertTrue(transaction.applied)
        self.assertTrue(transaction.rolled_back)
        self.assertFalse(engine.completed)

    def test_unsafe_patch_path_is_rejected(self):
        engine = FakeEngine()
        provider = FakeProvider({
            "action": "patch_set",
            "summary": "Unsafe change",
            "patches": [{
                "path": "ELON/main.py",
                "content": "unsafe = True\n",
                "expected_sha256": "mock-hash",
            }],
        })
        controller = self.make_controller(engine, provider)
        transaction = FakeTransaction()

        with patch(
            "engine.autonomous_controller.PatchTransaction",
            return_value=transaction,
        ):
            result = controller.develop_task(engine, 1)

        self.assertFalse(result["success"])
        self.assertFalse(transaction.applied)
        self.assertFalse(transaction.rolled_back)

    def test_completion_error_does_not_rollback_passing_code(self):
        engine = FakeEngine(completion_error=True)
        controller = self.make_controller(engine)
        transaction = FakeTransaction()

        with patch(
            "engine.autonomous_controller.PatchTransaction",
            return_value=transaction,
        ):
            result = controller.develop_task(engine, 1)

        self.assertFalse(result["success"])
        self.assertIn("Changes were retained", result["message"])
        self.assertTrue(transaction.applied)
        self.assertFalse(transaction.rolled_back)


    def test_core_baseline_then_requested_suite(self):
        engine = FakeEngine()
        controller = self.make_controller(engine)
        transaction = FakeTransaction()

        with patch(
            "engine.autonomous_controller.PatchTransaction",
            return_value=transaction,
        ):
            result = controller.develop_task(engine, 1, test_name="demo")

        self.assertTrue(result["success"])
        self.assertEqual(engine.test_names, ["core", "demo"])
        self.assertTrue(engine.completed)

    def test_allowed_file_parser(self):
        controller = self.make_controller(FakeEngine())
        plan = """## Allowed file
- workspace/repair_demo.py

## Required behavior
Validate inputs.
"""
        self.assertEqual(
            controller._extract_allowed_paths(plan),
            {"workspace/repair_demo.py"},
        )

    def test_allowed_file_parser_rejects_traversal(self):
        controller = self.make_controller(FakeEngine())
        plan = """## Allowed file
- ../outside.py
"""
        with self.assertRaises(ValueError):
            controller._extract_allowed_paths(plan)

    def test_context_contains_only_allowed_files(self):
        engine = FakeEngine()
        engine.policy.validate_modifiable_file = lambda path: None
        controller = self.make_controller(engine)
        controller._collect_safe_context = (
            AutonomousController._collect_safe_context.__get__(controller)
        )

        controller.repair_engine._collect_context = lambda: {
            "ELON/workspace/repair_demo.py": "safe = True",
            "ELON/workspace/other.py": "extra = True",
        }

        plan = """## Allowed file
- workspace/repair_demo.py
"""
        result = controller._collect_safe_context(engine, plan)

        self.assertEqual(
            set(result),
            {"workspace/repair_demo.py"},
        )

    def test_missing_allowed_file_stops_context_collection(self):
        engine = FakeEngine()
        engine.policy.validate_modifiable_file = lambda path: None
        controller = self.make_controller(engine)
        controller._collect_safe_context = (
            AutonomousController._collect_safe_context.__get__(controller)
        )
        controller.repair_engine._collect_context = lambda: {}

        plan = """## Allowed file
- workspace/repair_demo.py
"""
        with self.assertRaises(ValueError):
            controller._collect_safe_context(engine, plan)

if __name__ == "__main__":
    unittest.main()
