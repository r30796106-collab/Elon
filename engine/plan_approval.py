"""Persistent, hash-verified review and approval for validated plans."""

import errno
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

    def _fsync_audit_log(self):
        path = self.audit.path
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

        descriptor = None
        try:
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (
                errno.EINVAL,
                errno.ENOTSUP,
                errno.EBADF,
            ):
                raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

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
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temporary, path)

            # Persist the directory entry where supported so the atomic
            # replacement survives an abrupt process/device restart.
            directory_fd = None
            try:
                directory_fd = os.open(self.directory, os.O_RDONLY)
                os.fsync(directory_fd)
            except OSError as error:
                # Ignore only filesystems that explicitly lack directory fsync.
                if error.errno not in (
                    errno.EINVAL,
                    errno.ENOTSUP,
                    errno.EBADF,
                ):
                    raise
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
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

        task_id = record.get("task_id")
        if type(task_id) is not int or task_id < 1:
            raise PlanApprovalError("Invalid plan task identity.")

        reviewed_at = record.get("reviewed_at")
        if reviewed_at is not None and not isinstance(reviewed_at, str):
            raise PlanApprovalError("Invalid plan review timestamp.")
        if record["status"] == "pending" and reviewed_at is not None:
            raise PlanApprovalError("Pending plan has a review timestamp.")
        plan_steps = plan.get("steps")
        if not isinstance(plan_steps, list) or not plan_steps:
            raise PlanApprovalError("Invalid saved plan steps.")
        step_ids_list = []
        for step in plan_steps:
            if (
                not isinstance(step, dict)
                or isinstance(step.get("id"), bool)
                or not isinstance(step.get("id"), int)
                or step["id"] < 1
            ):
                raise PlanApprovalError("Invalid saved plan step identity.")
            step_ids_list.append(step["id"])
        if len(step_ids_list) != len(set(step_ids_list)):
            raise PlanApprovalError("Duplicate saved plan step identity.")
        step_ids = set(step_ids_list)

        execution = record.get("execution")
        if execution is not None:
            if not isinstance(execution, dict):
                raise PlanApprovalError("Invalid execution record.")

            if execution.get("state") not in (
                "claimed",
                "completed",
                "failed",
                "interrupted",
            ):
                raise PlanApprovalError("Invalid execution state.")

            started_at = execution.get("started_at")
            finished_at = execution.get("finished_at")
            if not isinstance(started_at, str) or not started_at:
                raise PlanApprovalError("Invalid execution start timestamp.")
            if finished_at is not None and not isinstance(finished_at, str):
                raise PlanApprovalError("Invalid execution finish timestamp.")
            if (
                execution["state"] == "claimed"
                and finished_at is not None
            ):
                raise PlanApprovalError("Claimed execution cannot be finished.")
            if (
                execution["state"] in ("completed", "failed", "interrupted")
                and (
                    not isinstance(finished_at, str)
                    or not finished_at
                )
            ):
                raise PlanApprovalError("Terminal execution is missing its finish timestamp.")

            completed_steps = execution.get("completed_steps", [])
            if not isinstance(completed_steps, list):
                raise PlanApprovalError("Invalid completed-step checkpoint.")
            if any(
                isinstance(step_id, bool)
                or not isinstance(step_id, int)
                or step_id not in step_ids
                for step_id in completed_steps
            ):
                raise PlanApprovalError("Invalid completed-step checkpoint.")
            if len(completed_steps) != len(set(completed_steps)):
                raise PlanApprovalError("Duplicate completed-step checkpoint.")

            failed_step = execution.get("failed_step")
            if (
                failed_step is not None
                and (
                    isinstance(failed_step, bool)
                    or not isinstance(failed_step, int)
                    or failed_step not in step_ids
                )
            ):
                raise PlanApprovalError("Invalid failed-step checkpoint.")

            last_completed = execution.get("last_completed_step")
            if (
                last_completed is not None
                and (
                    isinstance(last_completed, bool)
                    or not isinstance(last_completed, int)
                    or last_completed not in completed_steps
                )
            ):
                raise PlanApprovalError("Invalid last completed-step checkpoint.")

            checkpoint_state = execution.get("last_checkpoint_state")
            if checkpoint_state not in (None, "completed", "failed"):
                raise PlanApprovalError("Invalid checkpoint state.")
            checkpoint_at = execution.get("last_checkpoint_at")
            if checkpoint_at is not None and not isinstance(checkpoint_at, str):
                raise PlanApprovalError("Invalid checkpoint timestamp.")
            error_type = execution.get("last_error_type")
            if error_type is not None and (
                not isinstance(error_type, str) or len(error_type) > 100
            ):
                raise PlanApprovalError("Invalid checkpoint error type.")

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
        self._fsync_audit_log()

        return record

    def _audit_contains_event(self, event_name, plan_id):
        path = self.audit.path
        if not path.exists():
            return False

        if not self.audit.verify():
            raise PlanApprovalError("Audit log integrity check failed.")

        found = False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        raise PlanApprovalError(
                            f"Audit log is unreadable at line {line_number}."
                        ) from None

                    if not isinstance(event, dict):
                        raise PlanApprovalError(
                            f"Audit record {line_number} is invalid."
                        )

                    details = event.get("details")
                    if (
                        event.get("event") == event_name
                        and isinstance(details, dict)
                        and details.get("plan_id") == plan_id
                    ):
                        found = True
        except OSError as error:
            raise PlanApprovalError(
                f"Audit log could not be read: {error}"
            ) from None

        return found

    def _completed_steps_from_audit(self, plan_id):
        path = self.audit.path
        if not path.exists():
            return []
        if not self.audit.verify():
            raise PlanApprovalError("Audit log integrity check failed")

        completed = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        raise PlanApprovalError(
                            f"Audit log is unreadable at line {line_number}"
                        ) from None

                    if not isinstance(event, dict):
                        raise PlanApprovalError(
                            f"Audit record {line_number} is invalid"
                        )
                    details = event.get("details")
                    if (
                        event.get("event") != "PLAN_STEP_COMPLETED"
                        or not isinstance(details, dict)
                        or details.get("plan_id") != plan_id
                    ):
                        continue

                    step_id = details.get("step_id")
                    if (
                        isinstance(step_id, int)
                        and not isinstance(step_id, bool)
                        and step_id > 0
                        and step_id not in completed
                    ):
                        completed.append(step_id)
        except OSError as error:
            raise PlanApprovalError(
                f"Audit log could not be read: {error}"
            ) from None

        return completed

    def mark_interrupted(self, plan_id, confirm_stopped=False):
        if confirm_stopped is not True:
            raise PlanApprovalError(
                "Explicit confirmation is required: original process must be stopped."
            )

        record = self._load(plan_id)
        execution = record.get("execution")

        if execution is None:
            # A crash may occur after the exclusive lock is created but
            # before the claimed execution record is persisted.
            if record.get("status") != "approved":
                raise PlanApprovalError(
                    "Only an approved plan with an orphaned execution lock can be recovered."
                )
            started_at = None
        elif isinstance(execution, dict):
            if (
                execution.get("state") != "claimed"
                or execution.get("finished_at") is not None
                or not isinstance(execution.get("started_at"), str)
                or not execution.get("started_at")
            ):
                raise PlanApprovalError(
                    "Only an unfinished claimed execution can be recovered."
                )
            started_at = execution["started_at"]
        else:
            raise PlanApprovalError("Invalid execution record.")

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

        # Write the audit event first, then persist the terminal recovery
        # state. If the process stops between these operations, a retry sees
        # the existing event and completes the plan update without duplication.
        if not self._audit_contains_event("PLAN_EXECUTION_INTERRUPTED", plan_id):
            self.audit.append("PLAN_EXECUTION_INTERRUPTED", {
                "plan_id": plan_id,
                "task_id": record["task_id"],
                "plan_hash": record["plan_hash"],
                "lock_retained": True,
            })
        self._fsync_audit_log()

        interrupted_execution = (
            dict(execution) if isinstance(execution, dict) else {}
        )
        completed_steps = interrupted_execution.get("completed_steps", [])
        if not isinstance(completed_steps, list):
            completed_steps = []
        for step_id in self._completed_steps_from_audit(plan_id):
            if step_id not in completed_steps:
                completed_steps.append(step_id)
        interrupted_execution["completed_steps"] = completed_steps
        if completed_steps:
            interrupted_execution["last_completed_step"] = completed_steps[-1]
            interrupted_execution["last_checkpoint_state"] = "completed"

        interrupted_execution.update({
            "state": "interrupted",
            "started_at": started_at,
            "finished_at": self._now(),
        })
        record["execution"] = interrupted_execution

        self._write(self._path(plan_id), record)

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
        event_name = (
            "PLAN_APPROVED" if decision == "approved" else "PLAN_REJECTED"
        )

        if record["status"] not in ("pending", decision):
            raise PlanApprovalError("Plan has already been reviewed.")

        # Persist review evidence first so a retry can complete the record
        # transition without duplicating the approval/rejection event.
        if not self._audit_contains_event(event_name, plan_id):
            self.audit.append(
                event_name,
                {
                    "plan_id": plan_id,
                    "task_id": record["task_id"],
                    "plan_hash": record["plan_hash"],
                },
            )
        self._fsync_audit_log()

        if record["status"] == "pending":
            record["status"] = decision
            record["reviewed_at"] = self._now()
            self._write(self._path(plan_id), record)

        return record

    def approve(self, plan_id):
        return self._decide(plan_id, "approved")

    def reject(self, plan_id):
        return self._decide(plan_id, "rejected")
