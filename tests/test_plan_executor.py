import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.plan_approval import PlanApprovalManager
from engine.plan_executor import PlanExecutor, PlanExecutionError
from engine.ai_task_planner import AITaskPlanner
from engine.project_context import ProjectContextBuilder


class TestPlanExecutor(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        workspace = self.root / "workspace"
        workspace.mkdir(parents=True)

        (workspace / "calculator.py").write_text(
            "def add(a, b):\n"
            "    return a + b\n",
            encoding="utf-8",
        )

        self.engine = TaskEngine(self.root)
        self.manager = PlanApprovalManager(self.root)
        self.executor = PlanExecutor(self.engine, self.manager)
        self.planner = AITaskPlanner(self.root, provider=None)

        self.task_id = self.engine.create_task(
            "Calculator addition",
            "Inspect calculator addition implementation.",
        )
        self.task = self.engine.get_task(self.task_id)

        self.context = ProjectContextBuilder(self.root).build({
            "title": self.task["title"],
            "description": self.task["description"],
        })

        self.file_path = "workspace/calculator.py"

        self.assertTrue(self.context.get("success"))
        self.assertIn(
            self.file_path,
            [item["path"] for item in self.context["files"]],
        )

    def tearDown(self):
        self.temp.cleanup()

    def make_plan(self, steps=None):
        if steps is None:
            steps = [{
                "id": 1,
                "description": "Inspect calculator implementation",
                "type": "inspect",
                "files": [self.file_path],
                "tests": [],
            }]

        proposal = {
            "action": "plan",
            "objective": "Inspect calculator addition",
            "requirements": ["Review calculator implementation"],
            "steps": steps,
            "risks": [],
            "requires_approval": False,
        }

        return self.planner.validate_plan(proposal, self.context)

    def save_approved(self, plan=None, task_id=None):
        if plan is None:
            plan = self.make_plan()

        if task_id is None:
            task_id = self.task_id

        record = self.manager.save_plan(task_id, plan)
        self.manager.approve(record["plan_id"])
        return record

    def lock_path(self, plan_id):
        return self.manager.directory / f".execution_{plan_id}.lock"

    def test_approved_inspection_executes(self):
        record = self.save_approved()

        result = self.executor.execute(record["plan_id"])

        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            self.engine.get_task(self.task_id)["status"], "completed"
        )
        self.assertTrue(result["execution_performed"])
        self.assertEqual(len(result["steps"]), 1)
        self.assertTrue(result["steps"][0]["success"])
        self.assertTrue(self.lock_path(record["plan_id"]).exists())

    def test_failed_registered_test_marks_task_failed(self):
        plan = self.make_plan([{
            "id": 1,
            "description": "Run the registered core test suite",
            "type": "test",
            "files": [],
            "tests": ["core"],
        }])
        record = self.save_approved(plan)

        simulated_failure = {
            "success": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "simulated test failure",
        }

        with patch.object(
            self.engine,
            "run_registered_tests",
            return_value=simulated_failure,
        ):
            result = self.executor.execute(record["plan_id"])

        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            self.engine.get_task(self.task_id)["status"], "failed"
        )

    def test_replay_is_blocked(self):
        record = self.save_approved()

        self.executor.execute(record["plan_id"])

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

    def test_forged_extra_field_rejected_before_claim(self):
        plan = self.make_plan()
        plan["unexpected"] = "forged"

        record = self.save_approved(plan)

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

        self.assertFalse(self.lock_path(record["plan_id"]).exists())
        self.assertNotIn(
            "execution",
            self.manager.get_plan(record["plan_id"]),
        )

    def test_missing_task_rejected_before_claim(self):
        record = self.save_approved(task_id=999999)

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

        self.assertFalse(self.lock_path(record["plan_id"]).exists())

    def test_unregistered_test_rejected_before_claim(self):
        plan = self.make_plan([{
            "id": 1,
            "description": "Run the registered core test suite",
            "type": "test",
            "files": [],
            "tests": ["core"],
        }])

        # Simulate a forged record bypassing planner validation.
        plan["steps"][0]["tests"] = ["not_registered"]

        record = self.save_approved(plan)

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

        self.assertFalse(self.lock_path(record["plan_id"]).exists())

    def test_six_steps_rejected_before_claim(self):
        steps = [
            {
                "id": number,
                "description": "Inspect calculator implementation",
                "type": "inspect",
                "files": [self.file_path],
                "tests": [],
            }
            for number in range(1, 7)
        ]

        record = self.save_approved(self.make_plan(steps))

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

        self.assertFalse(self.lock_path(record["plan_id"]).exists())

    def test_modify_plan_rejected_before_claim(self):
        plan = self.make_plan([{
            "id": 1,
            "description": "Modify calculator implementation",
            "type": "modify",
            "files": [self.file_path],
            "tests": [],
        }])

        record = self.save_approved(plan)

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

        self.assertFalse(self.lock_path(record["plan_id"]).exists())

    def test_unapproved_plan_cannot_execute(self):
        plan = self.make_plan()
        record = self.manager.save_plan(self.task_id, plan)

        with self.assertRaises(PlanExecutionError):
            self.executor.execute(record["plan_id"])

        self.assertFalse(self.lock_path(record["plan_id"]).exists())


if __name__ == "__main__":
    unittest.main()
