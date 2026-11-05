import hashlib
import tempfile
import unittest
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction
from security.policy import PolicyError


class PatchTransactionTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.transaction = PatchTransaction(self.engine)

        self.a = self.root / "workspace" / "a.py"
        self.b = self.root / "workspace" / "b.py"

        self.a.write_text("value = 10\n")
        self.b.write_text("value = 20\n")

        self.task_id = self.engine.create_task("Multi-file test")

    def tearDown(self):
        self.temp.cleanup()

    def make_patches(self):
        return [
            {
                "path": "a.py",
                "content": "value = 100\n",
                "expected_sha256": hashlib.sha256(
                    self.a.read_bytes()
                ).hexdigest(),
            },
            {
                "path": "b.py",
                "content": "value = 200\n",
                "expected_sha256": hashlib.sha256(
                    self.b.read_bytes()
                ).hexdigest(),
            },
        ]

    def test_batch_updates_two_files_once(self):
        result = self.transaction.apply(
            self.task_id, self.make_patches()
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(self.a.read_text(), "value = 100\n")
        self.assertEqual(self.b.read_text(), "value = 200\n")

        task = self.engine.get_task(self.task_id)
        self.assertEqual(task["repair_attempts"], 1)

        for item in result:
            self.assertTrue(Path(item["backup"]).is_file())


    def test_single_file_patch_and_rollback(self):
        patch = [{
            "path": "a.py",
            "content": "value = 999\n",
            "expected_sha256": hashlib.sha256(
                self.a.read_bytes()
            ).hexdigest(),
        }]

        result = self.transaction.apply(self.task_id, patch)

        self.assertEqual(len(result), 1)
        self.assertEqual(self.a.read_text(), "value = 999\n")
        self.assertTrue(Path(result[0]["backup"]).is_file())
        self.assertEqual(
            self.engine.get_task(self.task_id)["repair_attempts"], 1
        )

        rollback_errors = self.transaction.rollback(result)

        self.assertEqual(rollback_errors, [])
        self.assertEqual(self.a.read_text(), "value = 10\n")
        self.assertEqual(self.b.read_text(), "value = 20\n")

    def test_wrong_hash_changes_nothing(self):
        patches = self.make_patches()
        patches[1]["expected_sha256"] = "wrong"

        with self.assertRaises(PolicyError):
            self.transaction.apply(self.task_id, patches)

        self.assertEqual(self.a.read_text(), "value = 10\n")
        self.assertEqual(self.b.read_text(), "value = 20\n")
        self.assertEqual(
            self.engine.get_task(self.task_id)["repair_attempts"], 0
        )

    def test_rollback_restores_both_files(self):
        result = self.transaction.apply(
            self.task_id, self.make_patches()
        )

        rollback_errors = self.transaction.rollback(result)

        self.assertEqual(rollback_errors, [])
        self.assertEqual(self.a.read_text(), "value = 10\n")
        self.assertEqual(self.b.read_text(), "value = 20\n")

    def test_duplicate_paths_rejected(self):
        patches = self.make_patches()
        patches[1]["path"] = "a.py"

        with self.assertRaises(PolicyError):
            self.transaction.apply(self.task_id, patches)

        self.assertEqual(self.a.read_text(), "value = 10\n")
        self.assertEqual(self.b.read_text(), "value = 20\n")


if __name__ == "__main__":
    unittest.main()
