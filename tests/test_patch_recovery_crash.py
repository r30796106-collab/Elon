import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecoveryProcessCrashTests(unittest.TestCase):

    def test_restart_after_crash_during_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = TaskEngine(root)
            transaction = PatchTransaction(engine)

            a = engine.workspace / "a.py"
            b = engine.workspace / "b.py"

            old_a = b"value = 10\n"
            old_b = b"value = 20\n"
            new_a = b"value = 100\n"
            new_b = b"value = 200\n"

            a.write_bytes(old_a)
            b.write_bytes(old_b)

            task_id = engine.create_task("Recovery process crash test")
            transaction_id = uuid.uuid4().hex

            records = [
                ("a.py", a, old_a, new_a),
                ("b.py", b, old_b, new_b),
            ]

            with engine._connect() as db:
                db.execute(
                    """INSERT INTO patch_transactions
                       (transaction_id, task_id, proposal_id, status,
                        repair_counted, created_at, updated_at, error)
                       VALUES (?, ?, NULL, 'applying', 0, 'now', 'now', NULL)""",
                    (transaction_id, task_id),
                )

                for index, (name, target, old, new) in enumerate(records):
                    backup_name = f"crash_recovery_{index}.bak"
                    (engine.backups / backup_name).write_bytes(old)
                    target.write_bytes(new)

                    db.execute(
                        """INSERT INTO patch_transaction_files
                           (transaction_id, ordinal, path, backup,
                            old_sha256, new_sha256, old_mode, state)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'replaced')""",
                        (
                            transaction_id,
                            index,
                            name,
                            backup_name,
                            transaction._hash(old),
                            transaction._hash(new),
                            target.stat().st_mode,
                        ),
                    )

            script = r'''
import os
import sys
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction

engine = TaskEngine(Path(sys.argv[1]))
transaction = PatchTransaction(engine)
transaction_id = sys.argv[2]

original_replace = os.replace
replacements = 0

def crash_after_first_restore(source, target):
    global replacements
    original_replace(source, target)
    replacements += 1
    if replacements == 1:
        os._exit(78)

os.replace = crash_after_first_restore

transaction.recover(
    transaction_id,
    confirm_stopped=True,
)
'''

            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                str(PROJECT_ROOT)
                + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(root),
                    transaction_id,
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(
                result.returncode,
                78,
                msg=f"Child did not stop at recovery crash point.\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )

            # Recovery restores in reverse order: b is restored first.
            self.assertEqual(a.read_bytes(), new_a)
            self.assertEqual(b.read_bytes(), old_b)

            # Simulate restart with a fresh engine and transaction manager.
            restarted_engine = TaskEngine(root)
            restarted_transaction = PatchTransaction(restarted_engine)

            with restarted_engine._connect() as db:
                status = db.execute(
                    "SELECT status FROM patch_transactions WHERE transaction_id = ?",
                    (transaction_id,),
                ).fetchone()[0]

            self.assertEqual(status, "applying")

            result = restarted_transaction.recover(
                transaction_id,
                confirm_stopped=True,
            )

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(result["files_checked"], 2)
            self.assertEqual(result["files_restored"], 1)

            self.assertEqual(a.read_bytes(), old_a)
            self.assertEqual(b.read_bytes(), old_b)

            self.assertEqual(
                restarted_engine.get_task(task_id)["repair_attempts"],
                0,
            )
            self.assertTrue(restarted_engine.audit.verify())


if __name__ == "__main__":
    unittest.main()
