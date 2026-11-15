import hashlib
import tempfile
import unittest
from pathlib import Path

from engine.task_engine import TaskEngine
from security.policy import PolicyError


class ElonCoreTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_task(self):
        task_id = self.engine.create_task("Test task", "Description")
        task = self.engine.get_task(task_id)

        self.assertEqual(task["title"], "Test task")
        self.assertEqual(task["status"], "pending")

    def test_patch_and_backup(self):
        target = self.root / "workspace" / "example.txt"
        target.write_text("original")

        task_id = self.engine.create_task("Modify file")
        old_hash = hashlib.sha256(b"original").hexdigest()

        result = self.engine.apply_patch(
            task_id,
            "example.txt",
            "updated",
            old_hash
        )

        self.assertTrue(result["success"])
        self.assertEqual(target.read_text(), "updated")
        self.assertTrue(Path(result["backup"]).exists())

    def test_wrong_hash_rejected(self):
        target = self.root / "workspace" / "example.txt"
        target.write_text("original")

        task_id = self.engine.create_task("Modify file")

        with self.assertRaises(PolicyError):
            self.engine.apply_patch(
                task_id, "example.txt", "updated", "wrong"
            )

        self.assertEqual(target.read_text(), "original")

    def test_path_traversal_blocked(self):
        target = self.root / "outside.txt"
        target.write_text("protected")

        task_id = self.engine.create_task("Security test")

        with self.assertRaises(PolicyError):
            self.engine.apply_patch(
                task_id,
                "../outside.txt",
                "changed",
                hashlib.sha256(b"protected").hexdigest()
            )

        self.assertEqual(target.read_text(), "protected")

    def test_repair_limit(self):
        target = self.root / "workspace" / "example.txt"
        target.write_text("version0")

        task_id = self.engine.create_task("Repair test")

        for i in range(5):
            old = target.read_bytes()
            digest = hashlib.sha256(old).hexdigest()

            self.engine.apply_patch(
                task_id, "example.txt",
                f"version{i+1}", digest, repair=True
            )

        old = target.read_bytes()

        with self.assertRaises(PolicyError):
            self.engine.apply_patch(
                task_id,
                "example.txt",
                "version6",
                hashlib.sha256(old).hexdigest(),
                repair=True
            )

    def test_audit_integrity(self):
        self.engine.create_task("Audit test")
        self.assertTrue(self.engine.audit_valid())

    def test_task_status_rejects_unknown_value(self):
        task_id = self.engine.create_task("Status validation")

        for invalid_status in ("running", None, [], {}):
            with self.subTest(status=invalid_status):
                with self.assertRaises(ValueError):
                    self.engine.update_task_status(task_id, invalid_status)

        self.assertEqual(self.engine.get_task(task_id)["status"], "pending")

    def test_cancelled_task_status_is_terminal(self):
        task_id = self.engine.create_task("Cancelled task")
        self.engine.update_task_status(task_id, "cancelled")

        with self.assertRaises(ValueError):
            self.engine.update_task_status(task_id, "pending")

        self.assertEqual(self.engine.get_task(task_id)["status"], "cancelled")

    def test_completed_task_status_is_terminal(self):
        task_id = self.engine.create_task("Completed task")
        self.engine.update_task_status(task_id, "completed")

        with self.assertRaises(ValueError):
            self.engine.update_task_status(task_id, "in_progress")

        self.assertEqual(self.engine.get_task(task_id)["status"], "completed")

    def test_failed_task_can_be_retried(self):
        task_id = self.engine.create_task("Retry failed task")
        self.engine.update_task_status(task_id, "failed")

        result = self.engine.update_task_status(task_id, "in_progress")

        self.assertEqual(result["status"], "in_progress")


    def test_concurrent_terminal_updates_cannot_overwrite_each_other(self):
        import threading

        task_id = self.engine.create_task("Concurrent terminal update")
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def update(status):
            barrier.wait(timeout=3)
            try:
                self.engine.update_task_status(task_id, status)
                result = (status, True)
            except ValueError:
                result = (status, False)
            with outcome_lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=update, args=("completed",)),
            threading.Thread(target=update, args=("cancelled",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sum(success for _, success in outcomes), 1)
        final_status = self.engine.get_task(task_id)["status"]
        self.assertIn(final_status, ("completed", "cancelled"))
        self.assertEqual(
            final_status,
            next(status for status, success in outcomes if success),
        )

    def test_task_status_audit_outbox_recovers_after_append_failure(self):
        import json

        task_id = self.engine.create_task("Audit outbox recovery")
        original_append = self.engine.audit.append

        def fail_append(event, details):
            if event == "TASK_STATUS_UPDATED":
                raise OSError("simulated audit disk failure")
            return original_append(event, details)

        self.engine.audit.append = fail_append
        with self.assertRaises(OSError):
            self.engine.update_task_status(task_id, "in_progress")

        # The status and its outbox event commit together even when delivery
        # fails. A fresh engine retries delivery during initialization.
        self.assertEqual(
            self.engine.get_task(task_id)["status"], "in_progress"
        )
        pending = self.engine._connect()
        with pending as db:
            count = db.execute(
                """SELECT COUNT(*) FROM task_status_audit_outbox
                WHERE task_id = ? AND delivered_at IS NULL""",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

        recovered = TaskEngine(self.root)
        recovered_again = TaskEngine(self.root)
        self.assertEqual(
            recovered.get_task(task_id)["status"], "in_progress"
        )

        records = [
            json.loads(line)
            for line in recovered.audit.path.read_text().splitlines()
            if line.strip()
        ]
        status_events = [
            record for record in records
            if record.get("event") == "TASK_STATUS_UPDATED"
            and record.get("details", {}).get("task_status_event_id")
        ]
        self.assertEqual(len(status_events), 1)
        self.assertTrue(recovered.audit_valid())

        with recovered_again._connect() as db:
            pending_count = db.execute(
                """SELECT COUNT(*) FROM task_status_audit_outbox
                WHERE task_id = ? AND delivered_at IS NULL""",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(pending_count, 0)

    def test_unregistered_test_blocked(self):
        with self.assertRaises(PolicyError):
            self.engine.run_registered_tests("unknown")


if __name__ == "__main__":
    unittest.main()
