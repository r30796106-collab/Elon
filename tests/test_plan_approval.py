import json
import os
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

    def test_plan_write_fsyncs_file_before_returning(self):
        record = self.save()
        record["durability_probe"] = True
        path = self.manager._path(record["plan_id"])

        from unittest.mock import patch
        with patch(
            "engine.plan_approval.os.fsync",
            wraps=os.fsync,
        ) as sync:
            self.manager._write(path, record)

        self.assertGreaterEqual(sync.call_count, 1)
        self.assertTrue(path.is_file())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(saved["durability_probe"])
        self.assertEqual(saved["plan_hash"], record["plan_hash"])

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

    def test_plan_approval_retries_after_write_failure_without_duplicate_audit(self):
        record = self.save()
        original_write = self.manager._write
        failed = {"value": False}

        def fail_write_once(path, value):
            if not failed["value"]:
                failed["value"] = True
                raise OSError("simulated plan approval write failure")
            return original_write(path, value)

        from unittest.mock import patch
        with patch.object(
            self.manager,
            "_write",
            side_effect=fail_write_once,
        ):
            with self.assertRaisesRegex(
                OSError,
                "plan approval write failure",
            ):
                self.manager.approve(record["plan_id"])

            self.assertEqual(
                self.manager.get_plan(record["plan_id"])["status"],
                "pending",
            )
            approved = self.manager.approve(record["plan_id"])

        self.assertEqual(approved["status"], "approved")
        audit_lines = [
            json.loads(line)
            for line in (self.root / "logs" / "audit.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        matching = [
            item for item in audit_lines
            if item.get("event") == "PLAN_APPROVED"
            and item.get("details", {}).get("plan_id") == record["plan_id"]
        ]
        self.assertEqual(len(matching), 1)
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

    def test_interrupted_recovery_preserves_completed_step_checkpoints(self):
        record, lock = self.make_claimed_execution()
        record["execution"]["completed_steps"] = [1, 2]
        record["execution"]["last_completed_step"] = 2
        record["execution"]["last_checkpoint_state"] = "completed"
        record["execution"]["last_checkpoint_at"] = self.manager._now()
        self.manager._write(
            self.manager._path(record["plan_id"]),
            record,
        )

        recovered = self.manager.mark_interrupted(
            record["plan_id"],
            confirm_stopped=True,
        )

        self.assertEqual(
            recovered["execution"]["state"],
            "interrupted",
        )
        self.assertEqual(
            recovered["execution"]["completed_steps"],
            [1, 2],
        )
        self.assertEqual(
            recovered["execution"]["last_completed_step"],
            2,
        )
        self.assertTrue(lock.exists())

    def test_recovery_marks_orphaned_execution_lock_interrupted(self):
        record = self.save()
        record["status"] = "approved"
        self.manager._write(
            self.manager._path(record["plan_id"]),
            record,
        )
        lock = self.manager.directory / f".execution_{record['plan_id']}.lock"
        lock.write_text(json.dumps({
            "plan_id": record["plan_id"],
            "task_id": record["task_id"],
            "plan_hash": record["plan_hash"],
        }), encoding="utf-8")

        result = self.manager.mark_interrupted(
            record["plan_id"],
            confirm_stopped=True,
        )

        self.assertEqual(result["execution"]["state"], "interrupted")
        self.assertIsNone(result["execution"]["started_at"])
        self.assertIsNotNone(result["execution"]["finished_at"])
        self.assertTrue(lock.exists())
        self.assertTrue(self.manager.audit.verify())

    def test_orphaned_execution_lock_recovery_requires_confirmation(self):
        record = self.save()
        record["status"] = "approved"
        self.manager._write(
            self.manager._path(record["plan_id"]),
            record,
        )
        lock = self.manager.directory / f".execution_{record['plan_id']}.lock"
        lock.write_text(json.dumps({
            "plan_id": record["plan_id"],
            "task_id": record["task_id"],
            "plan_hash": record["plan_hash"],
        }), encoding="utf-8")

        with self.assertRaises(PlanApprovalError):
            self.manager.mark_interrupted(record["plan_id"])

        self.assertNotIn(
            "execution",
            self.manager.get_plan(record["plan_id"]),
        )
        self.assertTrue(lock.exists())

    def test_interrupted_recovery_retries_after_plan_write_failure(self):
        record, lock = self.make_claimed_execution()
        original_write = self.manager._write
        calls = {"count": 0}

        def fail_once(path, value):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("simulated plan persistence failure")
            return original_write(path, value)

        from unittest.mock import patch
        with patch.object(self.manager, "_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.manager.mark_interrupted(
                    record["plan_id"],
                    confirm_stopped=True,
                )

            # The audit event is durable, while the plan is still claimed.
            self.assertEqual(
                self.manager.get_plan(record["plan_id"])["execution"]["state"],
                "claimed",
            )

            recovered = self.manager.mark_interrupted(
                record["plan_id"],
                confirm_stopped=True,
            )

        self.assertEqual(recovered["execution"]["state"], "interrupted")
        self.assertTrue(lock.exists())
        audit_lines = [
            json.loads(line)
            for line in (self.root / "logs" / "audit.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        matching = [
            item for item in audit_lines
            if item.get("event") == "PLAN_EXECUTION_INTERRUPTED"
            and item.get("details", {}).get("plan_id") == record["plan_id"]
        ]
        self.assertEqual(len(matching), 1)
        self.assertTrue(self.manager.audit.verify())

    def test_interrupted_audit_append_then_raise_is_idempotent(self):
        record, lock = self.make_claimed_execution()
        original_append = self.manager.audit.append
        failed = {"value": False}

        def append_then_fail(event, details):
            result = original_append(event, details)
            if (
                event == "PLAN_EXECUTION_INTERRUPTED"
                and not failed["value"]
            ):
                failed["value"] = True
                raise OSError("simulated post-append audit failure")
            return result

        from unittest.mock import patch
        with patch.object(
            self.manager.audit,
            "append",
            side_effect=append_then_fail,
        ):
            with self.assertRaises(OSError):
                self.manager.mark_interrupted(
                    record["plan_id"],
                    confirm_stopped=True,
                )

            saved = self.manager.get_plan(record["plan_id"])
            self.assertEqual(saved["execution"]["state"], "claimed")

            recovered = self.manager.mark_interrupted(
                record["plan_id"],
                confirm_stopped=True,
            )

        self.assertEqual(recovered["execution"]["state"], "interrupted")
        self.assertTrue(lock.exists())
        audit_lines = [
            json.loads(line)
            for line in (self.root / "logs" / "audit.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        matching = [
            item for item in audit_lines
            if item.get("event") == "PLAN_EXECUTION_INTERRUPTED"
            and item.get("details", {}).get("plan_id") == record["plan_id"]
        ]
        self.assertEqual(len(matching), 1)
        self.assertTrue(self.manager.audit.verify())

    def test_interrupted_audit_sync_failure_retries_without_duplicate_event(self):
        record, lock = self.make_claimed_execution()
        original_sync = self.manager._fsync_audit_log
        failed = {"value": False}

        def fail_sync_once():
            if not failed["value"]:
                failed["value"] = True
                raise OSError("simulated interruption audit sync failure")
            return original_sync()

        from unittest.mock import patch
        with patch.object(
            self.manager,
            "_fsync_audit_log",
            side_effect=fail_sync_once,
        ):
            with self.assertRaisesRegex(
                OSError,
                "interruption audit sync failure",
            ):
                self.manager.mark_interrupted(
                    record["plan_id"],
                    confirm_stopped=True,
                )

            self.assertEqual(
                self.manager.get_plan(record["plan_id"])["execution"]["state"],
                "claimed",
            )
            recovered = self.manager.mark_interrupted(
                record["plan_id"],
                confirm_stopped=True,
            )

        self.assertEqual(recovered["execution"]["state"], "interrupted")
        self.assertTrue(lock.exists())
        audit_lines = [
            json.loads(line)
            for line in (self.root / "logs" / "audit.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        matching = [
            item for item in audit_lines
            if item.get("event") == "PLAN_EXECUTION_INTERRUPTED"
            and item.get("details", {}).get("plan_id") == record["plan_id"]
        ]
        self.assertEqual(len(matching), 1)
        self.assertTrue(self.manager.audit.verify())

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
