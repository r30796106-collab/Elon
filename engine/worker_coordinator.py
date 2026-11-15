"""Explicit, non-executing worker leases for coordinating task ownership."""
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class WorkerLeaseError(RuntimeError):
    """Raised when a worker lease cannot safely be changed."""


class WorkerCoordinator:
    MIN_LEASE_SECONDS = 30
    MAX_LEASE_SECONDS = 3600

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.db_path = self.root / "elon.sqlite3"
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS worker_leases (
                    task_id INTEGER PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    lease_token TEXT NOT NULL UNIQUE,
                    claimed_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS worker_lease_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT ''
                )
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _timestamp(value):
        return value.isoformat()

    @classmethod
    def _validate_worker(cls, worker_id):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Worker ID must be non-empty text.")
        if len(worker_id) > 100:
            raise ValueError("Worker ID exceeds 100 characters.")
        return worker_id.strip()

    @classmethod
    def _validate_duration(cls, seconds):
        if type(seconds) is not int or not (
            cls.MIN_LEASE_SECONDS <= seconds <= cls.MAX_LEASE_SECONDS
        ):
            raise ValueError(
                f"Lease duration must be {cls.MIN_LEASE_SECONDS}–"
                f"{cls.MAX_LEASE_SECONDS} seconds."
            )

    def claim_next(self, worker_id, lease_seconds=300):
        """Atomically claim the oldest pending task; never execute its plan."""
        worker_id = self._validate_worker(worker_id)
        self._validate_duration(lease_seconds)
        now = self._now()
        expires = now + timedelta(seconds=lease_seconds)
        token = uuid.uuid4().hex

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT t.id, t.title, t.description, t.status
                FROM tasks AS t
                LEFT JOIN worker_leases AS w ON w.task_id = t.id
                WHERE t.status = 'pending' AND w.task_id IS NULL
                ORDER BY t.id ASC LIMIT 1
            """).fetchone()
            if row is None:
                return None

            task_id, title, description, status = row
            db.execute("""
                INSERT INTO worker_leases
                (task_id, worker_id, lease_token, claimed_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                task_id, worker_id, token, self._timestamp(now),
                self._timestamp(now), self._timestamp(expires),
            ))
            db.execute("""
                INSERT INTO worker_lease_events
                (task_id, worker_id, event, occurred_at, details)
                VALUES (?, ?, 'CLAIMED', ?, ?)
            """, (task_id, worker_id, self._timestamp(now), f"lease_seconds={lease_seconds}"))

        return {
            "task_id": task_id,
            "title": title,
            "description": description,
            "status": status,
            "worker_id": worker_id,
            "lease_token": token,
            "claimed_at": self._timestamp(now),
            "expires_at": self._timestamp(expires),
            "plan_execution": "NOT_STARTED",
        }

    def heartbeat(self, task_id, worker_id, lease_token, lease_seconds=300):
        worker_id = self._validate_worker(worker_id)
        self._validate_duration(lease_seconds)
        now = self._now()
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT expires_at FROM worker_leases
                WHERE task_id = ? AND worker_id = ? AND lease_token = ?
            """, (task_id, worker_id, lease_token)).fetchone()
            if row is None:
                raise WorkerLeaseError("Worker lease not found or ownership mismatch.")
            if datetime.fromisoformat(row[0]) <= now:
                raise WorkerLeaseError(
                    "Worker lease has expired; explicit stale-lease recovery is required."
                )
            db.execute("""
                UPDATE worker_leases SET heartbeat_at = ?, expires_at = ?
                WHERE task_id = ? AND worker_id = ? AND lease_token = ?
            """, (
                self._timestamp(now), self._timestamp(expires),
                task_id, worker_id, lease_token,
            ))
            db.execute("""
                INSERT INTO worker_lease_events
                (task_id, worker_id, event, occurred_at, details)
                VALUES (?, ?, 'HEARTBEAT', ?, ?)
            """, (task_id, worker_id, self._timestamp(now), f"lease_seconds={lease_seconds}"))
        return {"task_id": task_id, "worker_id": worker_id, "expires_at": self._timestamp(expires)}

    def release(self, task_id, worker_id, lease_token):
        worker_id = self._validate_worker(worker_id)
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""
                DELETE FROM worker_leases
                WHERE task_id = ? AND worker_id = ? AND lease_token = ?
            """, (task_id, worker_id, lease_token))
            if cursor.rowcount != 1:
                raise WorkerLeaseError("Worker lease not found or ownership mismatch.")
            db.execute("""
                INSERT INTO worker_lease_events
                (task_id, worker_id, event, occurred_at)
                VALUES (?, ?, 'RELEASED', ?)
            """, (task_id, worker_id, self._timestamp(now)))
        return {"task_id": task_id, "worker_id": worker_id, "released": True}

    def recover_expired(self, task_id, confirm_stopped=False):
        """Remove only an expired lease after operator confirms its worker stopped."""
        if confirm_stopped is not True:
            raise WorkerLeaseError(
                "Explicit confirmation that the original worker has stopped is required."
            )
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT worker_id, expires_at FROM worker_leases WHERE task_id = ?
            """, (task_id,)).fetchone()
            if row is None:
                raise WorkerLeaseError("No worker lease exists for this task.")
            worker_id, expires_at = row
            if datetime.fromisoformat(expires_at) > now:
                raise WorkerLeaseError("Worker lease has not expired.")
            cursor = db.execute(
                "DELETE FROM worker_leases WHERE task_id = ? AND expires_at = ?",
                (task_id, expires_at),
            )
            if cursor.rowcount != 1:
                raise WorkerLeaseError("Worker lease changed during recovery; retry inspection.")
            db.execute("""
                INSERT INTO worker_lease_events
                (task_id, worker_id, event, occurred_at, details)
                VALUES (?, ?, 'STALE_RECOVERED', ?, ?)
            """, (task_id, worker_id, self._timestamp(now), "operator_confirmed_stopped=true"))
        return {"task_id": task_id, "worker_id": worker_id, "recovered": True}

    def list_leases(self):
        now = self._timestamp(self._now())
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""
                SELECT w.task_id, t.title, t.status, w.worker_id,
                       w.claimed_at, w.heartbeat_at, w.expires_at,
                       CASE WHEN w.expires_at <= ? THEN 1 ELSE 0 END AS expired
                FROM worker_leases AS w
                JOIN tasks AS t ON t.id = w.task_id
                ORDER BY w.claimed_at, w.task_id
            """, (now,)).fetchall()
        return [dict(row) for row in rows]
