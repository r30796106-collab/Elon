import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PatchProcessCrashTests(unittest.TestCase):

    def test_process_death_after_first_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()

            (workspace / "a.py").write_text("value = 10\n")
            (workspace / "b.py").write_text("value = 20\n")

            script = r'''
import os
import sys
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction

root = Path(sys.argv[1])
engine = TaskEngine(root)
transaction = PatchTransaction(engine)

task_id = engine.create_task("Real process crash test")
a = engine.workspace / "a.py"
b = engine.workspace / "b.py"

patches = [
    {
        "path": "a.py",
        "content": "value = 100\n",
        "expected_sha256": transaction._hash(a.read_bytes()),
    },
    {
        "path": "b.py",
        "content": "value = 200\n",
        "expected_sha256": transaction._hash(b.read_bytes()),
    },
]

original_replace = os.replace
replacements = 0

def crash_after_first_replace(source, target):
    global replacements
    original_replace(source, target)
    replacements += 1
    if replacements == 1:
        os._exit(77)

os.replace = crash_after_first_replace
transaction.apply(task_id, patches)
'''

            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                str(PROJECT_ROOT)
                + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
            )

            result = subprocess.run(
                [sys.executable, "-c", script, str(root)],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(
                result.returncode,
                77,
                msg=f"Child did not stop at crash point.\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )

            a = workspace / "a.py"
            b = workspace / "b.py"

            self.assertEqual(a.read_text(), "value = 100\n")
            self.assertEqual(b.read_text(), "value = 20\n")

            # A journal must exist and still report an unfinished transaction.
            from engine.task_engine import TaskEngine
            engine = TaskEngine(root)

            with engine._connect() as db:
                transactions = db.execute(
                    "SELECT transaction_id, status FROM patch_transactions"
                ).fetchall()

                self.assertEqual(len(transactions), 1)
                transaction_id, status = transactions[0]
                self.assertEqual(status, "applying")

            recovery = __import__(
                "engine.patch_transaction",
                fromlist=["PatchTransaction"],
            ).PatchTransaction(engine)

            recovered = recovery.recover(
                transaction_id,
                confirm_stopped=True,
            )

            self.assertEqual(recovered["status"], "rolled_back")
            self.assertEqual(a.read_text(), "value = 10\n")
            self.assertEqual(b.read_text(), "value = 20\n")
            self.assertEqual(
                engine.get_task(1)["repair_attempts"],
                0,
            )
            self.assertTrue(engine.audit.verify())


if __name__ == "__main__":
    unittest.main()
