import copy
import tempfile
import unittest
from pathlib import Path

from engine.ai_task_planner import AITaskPlanner, TaskPlanError


class FakeProvider:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []

    def propose_task_plan(self, task, context):
        self.calls.append((task, context))
        return self.proposal


class AITaskPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        (self.root / "workspace").mkdir()
        (self.root / "workspace" / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )

        self.plan_data = {
            "action": "plan",
            "objective": "Improve calculator",
            "requirements": ["Inspect calculator implementation"],
            "steps": [
                {
                    "id": 1,
                    "description": "Inspect calculator implementation",
                    "type": "inspect",
                    "files": ["workspace/calculator.py"],
                    "tests": [],
                },
                {
                    "id": 2,
                    "description": "Validate calculator changes",
                    "type": "test",
                    "files": [],
                    "tests": ["demo"],
                },
            ],
            "risks": [],
            "requires_approval": False,
        }

        self.context = {
            "success": True,
            "files": [
                {
                    "path": "workspace/calculator.py",
                    "module": "workspace.calculator",
                }
            ],
            "tests": [],
        }

    def tearDown(self):
        self.temp.cleanup()

    def make_planner(self, proposal=None):
        provider = FakeProvider(
            proposal if proposal is not None else self.plan_data
        )
        return AITaskPlanner(self.root, provider), provider

    def test_valid_plan_is_accepted_without_execution(self):
        planner, provider = self.make_planner()

        result = planner.validate_plan(
            self.plan_data,
            self.context,
        )

        self.assertTrue(result["validated"])
        self.assertFalse(result["execution_performed"])
        self.assertEqual(len(result["steps"]), 2)

    def test_unknown_file_is_rejected(self):
        proposal = dict(self.plan_data)
        proposal["steps"] = [dict(self.plan_data["steps"][0])]
        proposal["steps"][0]["files"] = ["workspace/unknown.py"]

        planner, _ = self.make_planner()

        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_unregistered_test_is_rejected(self):
        proposal = dict(self.plan_data)
        proposal["steps"] = [dict(self.plan_data["steps"][1])]
        proposal["steps"][0]["tests"] = ["unregistered"]

        planner, _ = self.make_planner()

        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_path_traversal_is_rejected(self):
        context = {
            "files": [{"path": "../outside.py"}],
        }
        proposal = dict(self.plan_data)
        proposal["steps"] = [dict(self.plan_data["steps"][0])]
        proposal["steps"][0]["files"] = ["../outside.py"]

        planner, _ = self.make_planner()

        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, context)

    def test_non_sequential_step_ids_are_rejected(self):
        proposal = dict(self.plan_data)
        proposal["steps"] = [dict(self.plan_data["steps"][0])]
        proposal["steps"][0]["id"] = 2

        planner, _ = self.make_planner()

        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_restricted_operation_is_rejected(self):
        proposal = dict(self.plan_data)
        proposal["steps"] = [dict(self.plan_data["steps"][0])]
        proposal["steps"][0]["description"] = (
            "Run shell command to inspect files"
        )

        planner, _ = self.make_planner()

        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_modify_step_requires_context_file(self):
        proposal = dict(self.plan_data)
        proposal["steps"] = [{
            "id": 1,
            "description": "Modify calculator",
            "type": "modify",
            "files": ["workspace/calculator.py"],
            "tests": [],
        }]

        planner, _ = self.make_planner()

        result = planner.validate_plan(proposal, self.context)

        self.assertTrue(result["validated"])
        self.assertFalse(result["execution_performed"])

    def test_extra_fields_are_rejected(self):
        proposal = dict(self.plan_data)
        proposal["execute_now"] = True

        planner, _ = self.make_planner()

        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_planning_calls_provider_and_returns_validated_plan(self):
        planner, provider = self.make_planner()

        result = planner.plan({
            "title": "Improve calculator",
            "description": "Inspect calculator implementation",
        })

        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(result["validated"])
        self.assertFalse(result["execution_performed"])




    def test_malformed_file_entry_is_rejected_cleanly(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [proposal["steps"][0]]
        proposal["steps"][0]["files"] = [{"path": "workspace/calculator.py"}]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_malformed_test_entry_is_rejected_cleanly(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [proposal["steps"][1]]
        proposal["steps"][0]["tests"] = [["demo"]]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_malformed_step_type_is_rejected_cleanly(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [proposal["steps"][0]]
        proposal["steps"][0]["type"] = ["inspect"]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_restricted_objective_is_rejected(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["objective"] = "Run pip install to change the environment"
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_restricted_requirement_is_rejected(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["requirements"] = ["Run pip install"]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_protected_file_modification_is_rejected(self):
        protected = self.root / "core" / "audit.py"
        protected.parent.mkdir()
        protected.write_text("# protected audit module\n", encoding="utf-8")

        context = {
            "success": True,
            "files": [{"path": "core/audit.py"}],
            "tests": [],
        }
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [{
            "id": 1,
            "description": "Modify audit module",
            "type": "modify",
            "files": ["core/audit.py"],
            "tests": [],
        }]

        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, context)

    def test_restricted_risk_requires_approval(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["risks"] = ["Deployment must remain outside the plan."]
        planner, _ = self.make_planner()
        result = planner.validate_plan(proposal, self.context)
        self.assertTrue(result["requires_approval"])
        self.assertFalse(result["execution_performed"])


    def test_inspect_step_cannot_schedule_tests(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [copy.deepcopy(self.plan_data["steps"][0])]
        proposal["steps"][0]["tests"] = ["core"]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_review_step_cannot_schedule_tests(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [copy.deepcopy(self.plan_data["steps"][0])]
        proposal["steps"][0]["type"] = "review"
        proposal["steps"][0]["tests"] = ["core"]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)

    def test_test_step_cannot_specify_project_files(self):
        proposal = copy.deepcopy(self.plan_data)
        proposal["steps"] = [copy.deepcopy(self.plan_data["steps"][1])]
        proposal["steps"][0]["files"] = ["workspace/calculator.py"]
        planner, _ = self.make_planner()
        with self.assertRaises(TaskPlanError):
            planner.validate_plan(proposal, self.context)


if __name__ == "__main__":
    unittest.main()
