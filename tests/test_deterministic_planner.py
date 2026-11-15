import tempfile
import unittest
from pathlib import Path

from engine.deterministic_planner import DeterministicTaskPlanner
from engine.task_engine import TaskEngine
from engine.plan_approval import PlanApprovalManager
from engine.ai_task_planner import TaskPlanError


class DeterministicTaskPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "workspace").mkdir(parents=True)
        (self.root / "workspace" / "calculator.py").write_text(
            "def add(left, right):\n"
            "    return left + right\n",
            encoding="utf-8",
        )
        self.engine = TaskEngine(self.root)
        self.task_id = self.engine.create_task(
            "Calculator addition review",
            "Inspect the calculator addition implementation and tests.",
        )
        self.planner = DeterministicTaskPlanner(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_builds_valid_read_only_plan_without_provider(self):
        task = self.engine.get_task(self.task_id)
        plan = self.planner.plan({
            "title": task["title"],
            "description": task["description"],
        })

        self.assertTrue(plan["validated"])
        self.assertFalse(plan["execution_performed"])
        self.assertFalse(plan["requires_approval"])
        step_types = [step["type"] for step in plan["steps"]]
        self.assertIn("inspect", step_types)
        self.assertIn("analyze", step_types)
        self.assertIn("test", step_types)
        test_step = next(
            step for step in plan["steps"]
            if step["type"] == "test"
        )
        self.assertEqual(test_step["tests"], ["core"])

        saved = PlanApprovalManager(self.root).save_plan(
            self.task_id,
            plan,
        )
        self.assertEqual(saved["status"], "pending")

    def test_restricted_objective_is_rejected(self):
        with self.assertRaises(TaskPlanError):
            self.planner.plan({
                "title": "Delete all project files",
                "description": "This must never be executed.",
            })

    def test_plan_generation_does_not_require_ai_provider(self):
        self.assertIsNone(self.planner.validator.provider)
        plan = self.planner.plan({
            "title": "Review calculator",
            "description": "Inspect calculator source.",
        })
        self.assertTrue(plan["validated"])


if __name__ == "__main__":
    unittest.main()
