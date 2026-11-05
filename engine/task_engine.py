import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from security.policy import SecurityPolicy, PolicyError
from core.audit import AuditLog


class TaskEngine:
    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.workspace = self.root / "workspace"
        self.backups = self.root / "backups"
        self.logs = self.root / "logs"

        for folder in (
            self.workspace, self.backups, self.logs
        ):
            folder.mkdir(parents=True, exist_ok=True)

        self.policy = SecurityPolicy(self.root)
        self.audit = AuditLog(self.logs / "audit.jsonl")
        self.db_path = self.root / "elon.sqlite3"

        self._initialize_database()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize_database(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repair_attempts INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS patch_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    proposal_id TEXT,
                    status TEXT NOT NULL,
                    repair_counted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS patch_transaction_files (
                    transaction_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    backup TEXT,
                    old_sha256 TEXT NOT NULL,
                    new_sha256 TEXT NOT NULL,
                    old_mode INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (transaction_id, ordinal)
                )
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_patch_transactions_status
                ON patch_transactions(status)
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_patch_transactions_task
                ON patch_transactions(task_id)
            """)

    def create_task(self, title, description=""):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Task title cannot be empty")

        if len(title) > 200 or len(description) > 5000:
            raise ValueError("Task description too long")

        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO tasks
                (title, description, status, created_at)
                VALUES (?, ?, ?, ?)""",
                (
                    title.strip(),
                    description,
                    "pending",
                    datetime.now(timezone.utc).isoformat()
                )
            )
            task_id = cursor.lastrowid

        self.audit.append("TASK_CREATED", {"task_id": task_id})
        return task_id

    def get_task(self, task_id):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()

        return dict(row) if row else None

    def list_tasks(self):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM tasks ORDER BY id DESC"
            ).fetchall()

        return [dict(row) for row in rows]

    def run_registered_tests(self, test_name="core", timeout=120):
        modules = self.policy.validate_test(test_name)

        command = [
            sys.executable, "-m", "unittest", *modules, "-v"
        ]

        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False
            )

            success = result.returncode == 0

            self.audit.append(
                "TEST_EXECUTED",
                {
                    "test": test_name,
                    "returncode": result.returncode,
                    "success": success
                }
            )

            return {
                "success": success,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr
            }

        except subprocess.TimeoutExpired:
            self.audit.append(
                "TEST_TIMEOUT", {"test": test_name}
            )
            return {
                "success": False,
                "error": "Test execution timed out"
            }

    def apply_patch(
        self,
        task_id,
        relative_path,
        new_content,
        expected_sha256,
        repair=False
    ):
        task = self.get_task(task_id)

        if task is None:
            raise ValueError("Task does not exist")

        if task["status"] in ("completed", "cancelled"):
            raise PolicyError("Task is not modifiable")

        if not isinstance(new_content, str):
            raise ValueError("Content must be text")

        if len(new_content.encode()) > 2_000_000:
            raise PolicyError("Patch exceeds 2 MB limit")

        target = self.policy.validate_modifiable_file(relative_path)

        original = target.read_bytes()
        actual_hash = hashlib.sha256(original).hexdigest()

        if actual_hash != expected_sha256:
            raise PolicyError("File changed since inspection; patch rejected")

        if repair and task["repair_attempts"] >= self.policy.MAX_REPAIRS:
            raise PolicyError("Maximum repair attempts exceeded")

        import uuid
        backup_name = (
            f"task_{task_id}_{target.name}_{actual_hash[:12]}_"
            f"{uuid.uuid4().hex[:8]}.bak"
        )
        backup_path = self.backups / backup_name

        if backup_path.exists():
            raise PolicyError("Backup collision; refusing modification")

        shutil.copy2(target, backup_path)

        temporary = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=".elon_",
                delete=False
            ) as temp:
                temporary = Path(temp.name)
                temp.write(new_content.encode("utf-8"))

            os.chmod(temporary, target.stat().st_mode)
            os.replace(temporary, target)

        except Exception:
            if temporary and temporary.exists():
                temporary.unlink()
            raise

        with self._connect() as db:
            if repair:
                db.execute(
                    """UPDATE tasks
                    SET repair_attempts = repair_attempts + 1
                    WHERE id = ?""",
                    (task_id,)
                )

        new_hash = hashlib.sha256(target.read_bytes()).hexdigest()

        try:
            audit_path = str(target.relative_to(self.root))
        except ValueError:
            audit_path = str(target.relative_to(self.policy.home_root))

        self.audit.append(
            "PATCH_APPLIED",
            {
                "task_id": task_id,
                "path": audit_path,
                "old_hash": actual_hash,
                "new_hash": new_hash,
                "backup": backup_name,
                "repair": repair
            }
        )

        return {
            "success": True,
            "backup": str(backup_path),
            "old_sha256": actual_hash,
            "new_sha256": new_hash
        }

    def update_task_status(self, task_id, status):
        """Set a task status using an explicit allowlist."""
        allowed = {"pending", "in_progress", "completed", "failed", "cancelled"}
        if status not in allowed:
            raise ValueError("Invalid task status")

        task = self.get_task(task_id)
        if task is None:
            raise ValueError("Task does not exist")

        if task["status"] == "cancelled" and status != "cancelled":
            raise ValueError("Cancelled task status cannot be changed")

        if task["status"] == "completed" and status != "completed":
            raise ValueError("Completed task status cannot be changed")

        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (status, task_id),
            )

        if cursor.rowcount != 1:
            raise ValueError("Task status update failed")

        self.audit.append(
            "TASK_STATUS_UPDATED",
            {"task_id": task_id, "status": status},
        )
        return self.get_task(task_id)

    def complete_task(self, task_id):
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE tasks SET status = 'completed'
                WHERE id = ? AND status NOT IN ('completed', 'cancelled')""",
                (task_id,)
            )

        if cursor.rowcount == 0:
            raise ValueError("Task unavailable or already completed")

        self.audit.append("TASK_COMPLETED", {"task_id": task_id})

    def audit_valid(self):
        return self.audit.verify()
