import json
import tempfile
import unittest
from pathlib import Path

from engine.plan_approval import (
    PlanApprovalManager,
    PlanApprovalError,
)


VALID_PLAN = {
    "action": "plan",
    "objective": "Add calculator",
    "requirements": ["Support addition"],
    "steps": [
        {
            "id": 1,
            "description": "Inspect calculator",
            "type": "inspect",
            "files": [],
            "tests": [],
        }
    ],
    "risks": [],
    "requires_approval": False,
    "validated": True,
    "execution_performed": False,
}


class TestPlanApproval(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = PlanApprovalManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        return self.manager.save_plan(1, VALID_PLAN)

    def test_save_plan_pending(self):
        record = self.save()
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["plan_hash"])
        self.assertEqual(
            self.manager.get_plan(record["plan_id"])["status"],
            "pending",
        )

    def test_approve_plan(self):
        record = self.save()
        result = self.manager.approve(record["plan_id"])
        self.assertEqual(result["status"], "approved")
        self.assertTrue(self.manager.audit.verify())

    def test_reject_plan(self):
        record = self.save()
        result = self.manager.reject(record["plan_id"])
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(self.manager.audit.verify())

    def test_cannot_decide_twice(self):
        record = self.save()
        self.manager.approve(record["plan_id"])
        with self.assertRaises(PlanApprovalError):
            self.manager.reject(record["plan_id"])

    def test_tampered_plan_rejected(self):
        record = self.save()
        path = self.root / "plans" / f"plan_{record['plan_id']}.json"
        data = json.loads(path.read_text())
        data["plan"]["objective"] = "Tampered objective"
        path.write_text(json.dumps(data))

        with self.assertRaises(PlanApprovalError):
            self.manager.get_plan(record["plan_id"])

    def test_unvalidated_plan_rejected(self):
        plan = dict(VALID_PLAN)
        plan["validated"] = False
        with self.assertRaises(PlanApprovalError):
            self.manager.save_plan(1, plan)

    def test_executed_plan_rejected(self):
        plan = dict(VALID_PLAN)
        plan["execution_performed"] = True
        with self.assertRaises(PlanApprovalError):
            self.manager.save_plan(1, plan)

    def test_invalid_plan_id_rejected(self):
        with self.assertRaises(PlanApprovalError):
            self.manager.get_plan("../invalid")

    def test_missing_plan_rejected(self):
        with self.assertRaises(PlanApprovalError):
            self.manager.get_plan("a" * 32)

    def test_list_plans(self):
        self.save()
        self.save()
        self.assertEqual(len(self.manager.list_plans()), 2)


    def make_claimed_execution(self):
        record = self.save()
        lock = self.manager.directory / f".execution_{record['plan_id']}.lock"
        lock.write_text(json.dumps({
            "plan_id": record["plan_id"],
            "task_id": record["task_id"],
            "plan_hash": record["plan_hash"],
        }), encoding="utf-8")

        record["status"] = "approved"
        record["execution"] = {
            "state": "claimed",
            "started_at": self.manager._now(),
            "finished_at": None,
        }
        self.manager._write(
            self.manager._path(record["plan_id"]),
            record,
        )
        return record, lock

    def test_recovery_marks_claimed_execution_interrupted(self):
        record, lock = self.make_claimed_execution()

        result = self.manager.mark_interrupted(
            record["plan_id"],
            confirm_stopped=True,
        )

        self.assertEqual(result["execution"]["state"], "interrupted")
        self.assertIsNotNone(result["execution"]["finished_at"])
        self.assertTrue(lock.exists())
        self.assertTrue(self.manager.audit.verify())

    def test_recovery_requires_explicit_confirmation(self):
        record, lock = self.make_claimed_execution()

        with self.assertRaises(PlanApprovalError):
            self.manager.mark_interrupted(record["plan_id"])

        self.assertEqual(
            self.manager.get_plan(record["plan_id"])["execution"]["state"],
            "claimed",
        )
        self.assertTrue(lock.exists())

    def test_recovery_rejects_missing_lock(self):
        record, lock = self.make_claimed_execution()
        lock.unlink()

        with self.assertRaises(PlanApprovalError):
            self.manager.mark_interrupted(
                record["plan_id"],
                confirm_stopped=True,
            )

        self.assertEqual(
            self.manager.get_plan(record["plan_id"])["execution"]["state"],
            "claimed",
        )

    def test_recovery_rejects_lock_identity_mismatch(self):
        record, lock = self.make_claimed_execution()
        lock.write_text(json.dumps({
            "plan_id": record["plan_id"],
            "task_id": 999999,
            "plan_hash": record["plan_hash"],
        }), encoding="utf-8")

        with self.assertRaises(PlanApprovalError):
            self.manager.mark_interrupted(
                record["plan_id"],
                confirm_stopped=True,
            )

        self.assertEqual(
            self.manager.get_plan(record["plan_id"])["execution"]["state"],
            "claimed",
        )

    def test_recovery_cannot_be_repeated(self):
        record, lock = self.make_claimed_execution()

        self.manager.mark_interrupted(
            record["plan_id"],
            confirm_stopped=True,
        )

        with self.assertRaises(PlanApprovalError):
            self.manager.mark_interrupted(
                record["plan_id"],
                confirm_stopped=True,
            )

        self.assertTrue(lock.exists())

if __name__ == "__main__":
    unittest.main()
