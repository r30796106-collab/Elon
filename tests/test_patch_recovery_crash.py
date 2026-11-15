import json
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

    def _create_interrupted_case(self, root):
        engine = TaskEngine(root)
        transaction = PatchTransaction(engine)
        target = engine.workspace / "a.py"
        old_data = b"value = 10\\n"
        new_data = b"value = 100\\n"
        target.write_bytes(new_data)
        task_id = engine.create_task("Recovery audit outbox crash test")
        transaction_id = uuid.uuid4().hex
        backup_name = "recovery_audit_outbox.bak"
        (engine.backups / backup_name).write_bytes(old_data)

        with engine._connect() as db:
            db.execute(
                """INSERT INTO patch_transactions
                   (transaction_id, task_id, proposal_id, status,
                    repair_counted, created_at, updated_at, error)
                   VALUES (?, ?, NULL, 'applying', 0, 'now', 'now', NULL)""",
                (transaction_id, task_id),
            )
            db.execute(
                """INSERT INTO patch_transaction_files
                   (transaction_id, ordinal, path, backup,
                    old_sha256, new_sha256, old_mode, state)
                   VALUES (?, 0, 'a.py', ?, ?, ?, ?, 'replaced')""",
                (
                    transaction_id,
                    backup_name,
                    transaction._hash(old_data),
                    transaction._hash(new_data),
                    target.stat().st_mode,
                ),
            )

        return engine, target, old_data, task_id, transaction_id

    def _run_recovery_crash_child(
        self, root, transaction_id, exit_code, after_append
    ):
        script = r'''
import os
import sys
from pathlib import Path
from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction

engine = TaskEngine(Path(sys.argv[1]))
transaction = PatchTransaction(engine)
transaction_id = sys.argv[2]
exit_code = int(sys.argv[3])
after_append = sys.argv[4] == "1"
original_append = engine.audit.append

def crash_at_audit_boundary(event, details):
    if event == "PATCH_TRANSACTION_RECOVERED":
        if after_append:
            original_append(event, details)
        os._exit(exit_code)
    return original_append(event, details)

engine.audit.append = crash_at_audit_boundary
transaction.recover(transaction_id, confirm_stopped=True)
'''
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(PROJECT_ROOT)
            + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        )
        return subprocess.run(
            [
                sys.executable, "-c", script, str(root), transaction_id,
                str(exit_code), "1" if after_append else "0",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _assert_one_recovery_audit_event(self, engine, transaction_id):
        records = [
            json.loads(line)
            for line in (engine.logs / "audit.jsonl").read_text().splitlines()
        ]
        matching = [
            record for record in records
            if record.get("event") == "PATCH_TRANSACTION_RECOVERED"
            and record.get("details", {}).get("transaction_id") == transaction_id
        ]
        self.assertEqual(len(matching), 1)
        with engine._connect() as db:
            delivered_at = db.execute(
                """SELECT delivered_at FROM patch_recovery_audit_outbox
                   WHERE transaction_id = ?""",
                (transaction_id,),
            ).fetchone()[0]
        self.assertIsNotNone(delivered_at)
        self.assertTrue(engine.audit.verify())

    def test_restart_after_crash_before_recovery_audit_append(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine, target, old_data, task_id, transaction_id = (
                self._create_interrupted_case(root)
            )
            result = self._run_recovery_crash_child(
                root, transaction_id, 79, after_append=False
            )
            self.assertEqual(result.returncode, 79, result.stderr)
            self.assertEqual(target.read_bytes(), old_data)

            restarted_engine = TaskEngine(root)
            PatchTransaction(restarted_engine)
            with restarted_engine._connect() as db:
                status = db.execute(
                    "SELECT status FROM patch_transactions WHERE transaction_id = ?",
                    (transaction_id,),
                ).fetchone()[0]
            self.assertEqual(status, "rolled_back")
            self.assertEqual(
                restarted_engine.get_task(task_id)["repair_attempts"], 0
            )
            self._assert_one_recovery_audit_event(
                restarted_engine, transaction_id
            )

            # A later restart must not append the same event again.
            PatchTransaction(TaskEngine(root))
            self._assert_one_recovery_audit_event(
                restarted_engine, transaction_id
            )

    def test_restart_after_audit_append_before_outbox_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine, target, old_data, _task_id, transaction_id = (
                self._create_interrupted_case(root)
            )
            result = self._run_recovery_crash_child(
                root, transaction_id, 80, after_append=True
            )
            self.assertEqual(result.returncode, 80, result.stderr)
            self.assertEqual(target.read_bytes(), old_data)

            restarted_engine = TaskEngine(root)
            PatchTransaction(restarted_engine)
            self._assert_one_recovery_audit_event(
                restarted_engine, transaction_id
            )


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
