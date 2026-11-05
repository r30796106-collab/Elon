"""Persistent, hash-verified review and approval for validated plans."""

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.audit import AuditLog


class PlanApprovalError(ValueError):
    """Raised when a plan cannot be safely reviewed."""


class PlanApprovalManager:
    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.directory = self.root / "plans"
        self.audit = AuditLog(self.root / "logs" / "audit.jsonl")

        if self.directory.is_symlink():
            raise PlanApprovalError("Plans directory cannot be a symlink.")

        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _digest(plan):
        return AuditLog.digest(plan)

    def _path(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", plan_id
        ):
            raise PlanApprovalError("Invalid plan ID.")

        path = self.directory / f"plan_{plan_id}.json"

        if path.is_symlink():
            raise PlanApprovalError("Plan symlinks are not permitted.")

        return path

    def _write(self, path, record):
        temporary = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=".plan_",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")

            os.replace(temporary, path)
        except Exception:
            if temporary and temporary.exists():
                temporary.unlink()
            raise

    def _load(self, plan_id):
        path = self._path(plan_id)

        if not path.is_file():
            raise PlanApprovalError("Plan does not exist.")

        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise PlanApprovalError("Plan record is unreadable.") from None

        if not isinstance(record, dict):
            raise PlanApprovalError("Invalid plan record.")

        plan = record.get("plan")

        if not isinstance(plan, dict):
            raise PlanApprovalError("Invalid saved plan.")

        if record.get("plan_hash") != self._digest(plan):
            raise PlanApprovalError("Plan integrity check failed.")

        if plan.get("validated") is not True:
            raise PlanApprovalError("Plan was not validated.")

        if plan.get("execution_performed") is not False:
            raise PlanApprovalError("Plan execution state is invalid.")

        if record.get("plan_id") != plan_id:
            raise PlanApprovalError("Plan identity mismatch.")

        if record.get("status") not in ("pending", "approved", "rejected"):
            raise PlanApprovalError("Invalid approval status.")

        return record

    def save_plan(self, task_id, plan):
        if type(task_id) is not int or task_id < 1:
            raise PlanApprovalError("Invalid task ID.")

        if not isinstance(plan, dict):
            raise PlanApprovalError("Plan must be an object.")

        if plan.get("action") != "plan":
            raise PlanApprovalError("Unsupported plan action.")

        if plan.get("validated") is not True:
            raise PlanApprovalError("Only validated plans can be saved.")

        if plan.get("execution_performed") is not False:
            raise PlanApprovalError("Executed plans cannot be submitted.")

        # Ensure the plan can be safely represented as JSON.
        try:
            json.dumps(plan, ensure_ascii=False)
        except (TypeError, ValueError):
            raise PlanApprovalError("Plan is not JSON serializable.") from None

        plan_id = uuid.uuid4().hex
        path = self._path(plan_id)

        record = {
            "plan_id": plan_id,
            "task_id": task_id,
            "plan": plan,
            "plan_hash": self._digest(plan),
            "status": "pending",
            "created_at": self._now(),
            "reviewed_at": None,
        }

        self._write(path, record)
        self.audit.append("PLAN_SUBMITTED_FOR_REVIEW", {
            "plan_id": plan_id,
            "task_id": task_id,
            "plan_hash": record["plan_hash"],
        })

        return record

    def mark_interrupted(self, plan_id, confirm_stopped=False):
        if confirm_stopped is not True:
            raise PlanApprovalError(
                "Explicit confirmation is required: original process must be stopped."
            )

        record = self._load(plan_id)
        execution = record.get("execution")

        if not isinstance(execution, dict):
            raise PlanApprovalError("Plan has no execution record.")

        if (
            execution.get("state") != "claimed"
            or execution.get("finished_at") is not None
            or not isinstance(execution.get("started_at"), str)
            or not execution.get("started_at")
        ):
            raise PlanApprovalError(
                "Only an unfinished claimed execution can be recovered."
            )

        lock = self.directory / f".execution_{plan_id}.lock"

        if lock.is_symlink() or not lock.is_file():
            raise PlanApprovalError(
                "Execution lock is missing or unsafe; recovery refused."
            )

        try:
            if lock.stat().st_size > 4096:
                raise PlanApprovalError("Execution lock is unexpectedly large.")

            lock_data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise PlanApprovalError("Execution lock is unreadable.") from None

        expected_lock = {
            "plan_id": plan_id,
            "task_id": record["task_id"],
            "plan_hash": record["plan_hash"],
        }

        if lock_data != expected_lock:
            raise PlanApprovalError(
                "Execution lock identity does not match the saved plan."
            )

        record["execution"] = {
            "state": "interrupted",
            "started_at": execution["started_at"],
            "finished_at": self._now(),
        }

        self._write(self._path(plan_id), record)

        self.audit.append("PLAN_EXECUTION_INTERRUPTED", {
            "plan_id": plan_id,
            "task_id": record["task_id"],
            "plan_hash": record["plan_hash"],
            "lock_retained": True,
        })

        return record

    def get_plan(self, plan_id):
        return self._load(plan_id)

    def list_plans(self):
        results = []

        for path in sorted(self.directory.glob("plan_*.json")):
            if path.is_symlink():
                continue

            plan_id = path.stem.removeprefix("plan_")
            results.append(self._load(plan_id))

        return results

    def _decide(self, plan_id, decision):
        record = self._load(plan_id)

        if record["status"] != "pending":
            raise PlanApprovalError("Plan has already been reviewed.")

        record["status"] = decision
        record["reviewed_at"] = self._now()

        path = self._path(plan_id)
        self._write(path, record)

        self.audit.append(
            "PLAN_APPROVED" if decision == "approved" else "PLAN_REJECTED",
            {
                "plan_id": plan_id,
                "task_id": record["task_id"],
                "plan_hash": record["plan_hash"],
            },
        )

        return record

    def approve(self, plan_id):
        return self._decide(plan_id, "approved")

    def reject(self, plan_id):
        return self._decide(plan_id, "rejected")
