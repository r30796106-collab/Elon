import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction
from security.policy import PolicyError


class PatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.transaction = PatchTransaction(self.engine)

        self.a = self.engine.workspace / "a.py"
        self.b = self.engine.workspace / "b.py"

        self.old_a = b"value = 10\n"
        self.old_b = b"value = 20\n"
        self.new_a = b"value = 100\n"
        self.new_b = b"value = 200\n"

        self.a.write_bytes(self.old_a)
        self.b.write_bytes(self.old_b)

        self.task_id = self.engine.create_task("Recovery test")
        self.transaction_id = uuid.uuid4().hex

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def create_interrupted(self):
        records = [
            ("a.py", self.a, self.old_a, self.new_a),
            ("b.py", self.b, self.old_b, self.new_b),
        ]

        with self.engine._connect() as db:
            db.execute(
                """INSERT INTO patch_transactions
                   (transaction_id, task_id, proposal_id, status,
                    repair_counted, created_at, updated_at, error)
                   VALUES (?, ?, NULL, 'applying', 0, 'now', 'now', NULL)""",
                (self.transaction_id, self.task_id),
            )

            for index, (name, target, old, new) in enumerate(records):
                backup_name = f"recovery_{index}.bak"
                (self.engine.backups / backup_name).write_bytes(old)

                target.write_bytes(new)

                db.execute(
                    """INSERT INTO patch_transaction_files
                       (transaction_id, ordinal, path, backup,
                        old_sha256, new_sha256, old_mode, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'replaced')""",
                    (
                        self.transaction_id,
                        index,
                        name,
                        backup_name,
                        self.digest(old),
                        self.digest(new),
                        target.stat().st_mode,
                    ),
                )

    def test_confirmation_required(self):
        self.create_interrupted()

        with self.assertRaises(PolicyError):
            self.transaction.recover(self.transaction_id)

        self.assertEqual(self.a.read_bytes(), self.new_a)
        self.assertEqual(self.b.read_bytes(), self.new_b)

    def test_recovers_interrupted_transaction(self):
        self.create_interrupted()

        result = self.transaction.recover(
            self.transaction_id,
            confirm_stopped=True,
        )

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["files_restored"], 2)
        self.assertEqual(self.a.read_bytes(), self.old_a)
        self.assertEqual(self.b.read_bytes(), self.old_b)
        self.assertEqual(
            self.engine.get_task(self.task_id)["repair_attempts"], 0
        )

        with self.engine._connect() as db:
            status = db.execute(
                "SELECT status FROM patch_transactions WHERE transaction_id = ?",
                (self.transaction_id,),
            ).fetchone()[0]

        self.assertEqual(status, "rolled_back")
        self.assertTrue(self.engine.audit.verify())

    def test_already_restored_file_is_skipped(self):
        self.create_interrupted()
        self.a.write_bytes(self.old_a)

        result = self.transaction.recover(
            self.transaction_id,
            confirm_stopped=True,
        )

        self.assertEqual(result["files_restored"], 1)
        self.assertEqual(self.a.read_bytes(), self.old_a)
        self.assertEqual(self.b.read_bytes(), self.old_b)

    def test_unexpected_file_hash_aborts_all_restoration(self):
        self.create_interrupted()
        self.b.write_bytes(b"external modification\n")

        with self.assertRaises(PolicyError):
            self.transaction.recover(
                self.transaction_id,
                confirm_stopped=True,
            )

        self.assertEqual(self.a.read_bytes(), self.new_a)
        self.assertEqual(self.b.read_bytes(), b"external modification\n")

    def test_tampered_backup_aborts_all_restoration(self):
        self.create_interrupted()
        (self.engine.backups / "recovery_1.bak").write_bytes(
            b"tampered backup"
        )

        with self.assertRaises(PolicyError):
            self.transaction.recover(
                self.transaction_id,
                confirm_stopped=True,
            )

        self.assertEqual(self.a.read_bytes(), self.new_a)
        self.assertEqual(self.b.read_bytes(), self.new_b)

    def test_applied_transaction_cannot_be_recovered(self):
        self.create_interrupted()

        with self.engine._connect() as db:
            db.execute(
                """UPDATE patch_transactions
                   SET status = 'applied', repair_counted = 1
                   WHERE transaction_id = ?""",
                (self.transaction_id,),
            )

        with self.assertRaises(PolicyError):
            self.transaction.recover(
                self.transaction_id,
                confirm_stopped=True,
            )

        self.assertEqual(self.a.read_bytes(), self.new_a)
        self.assertEqual(self.b.read_bytes(), self.new_b)

    def test_invalid_transaction_id_rejected(self):
        with self.assertRaises(PolicyError):
            self.transaction.recover(
                "../invalid",
                confirm_stopped=True,
            )


if __name__ == "__main__":
    unittest.main()
