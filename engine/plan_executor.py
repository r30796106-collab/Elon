"""Bounded, audited execution of approved, read-only development plans."""

import hashlib
import json
import os
from pathlib import Path

from engine.ai_task_planner import AITaskPlanner, TaskPlanError
from engine.project_context import ProjectContextBuilder
from engine.project_intelligence import ProjectScanner


class PlanExecutionError(RuntimeError):
    """Raised when a plan cannot safely execute."""


class PlanExecutor:
    MAX_STEPS = 5
    MAX_FILE_BYTES = 64 * 1024

    def __init__(self, engine, approval_manager):
        self.engine = engine
        self.manager = approval_manager
        self.root = Path(engine.root).resolve()
        self.context = ProjectContextBuilder(self.root)
        self.scanner = ProjectScanner(self.root)
        self.planner = AITaskPlanner(self.root, provider=None)

    def _audit(self, event, details):
        self.engine.audit.append(event, details)

    def _fresh_validate(self, record, task):
        plan = record.get("plan")

        if not isinstance(plan, dict):
            raise PlanExecutionError("Invalid plan object.")

        expected_fields = (
            self.planner.ALLOWED_ACTION_FIELDS
            | {"validated", "execution_performed"}
        )

        if set(plan) != expected_fields:
            raise PlanExecutionError("Unexpected plan fields.")

        if plan.get("validated") is not True:
            raise PlanExecutionError("Plan is not validated.")

        if plan.get("execution_performed") is not False:
            raise PlanExecutionError("Plan execution flag is invalid.")

        task_data = {
            "title": task.get("title"),
            "description": task.get("description", ""),
        }

        fresh_context = self.context.build(task_data)

        if not fresh_context.get("success"):
            raise PlanExecutionError("Fresh project context could not be built.")

        original_fields = {
            key: plan[key]
            for key in self.planner.ALLOWED_ACTION_FIELDS
        }

        try:
            validated = self.planner.validate_plan(
                original_fields,
                fresh_context,
            )
        except (TaskPlanError, ValueError, OSError):
            raise PlanExecutionError(
                "Plan failed fresh security validation."
            ) from None

        if self.manager._digest(validated) != self.manager._digest(plan):
            raise PlanExecutionError(
                "Stored plan differs from its freshly validated version."
            )

        return validated

    def _claim(self, record):
        plan_id = record["plan_id"]

        if record.get("execution") is not None:
            raise PlanExecutionError(
                "Plan already has an execution record. Replay is blocked."
            )

        lock = self.manager.directory / f".execution_{plan_id}.lock"

        try:
            fd = os.open(
                str(lock),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            raise PlanExecutionError(
                "This plan has already been claimed. Replay is blocked."
            ) from None

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({
                    "plan_id": plan_id,
                    "task_id": record["task_id"],
                    "plan_hash": record["plan_hash"],
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())

            record["execution"] = {
                "state": "claimed",
                "started_at": self.manager._now(),
                "finished_at": None,
            }

            self.manager._write(
                self.manager._path(plan_id),
                record,
            )

        except Exception:
            try:
                lock.unlink()
            except OSError:
                pass
            raise

    PREVIEW_CHARS = 1200

    def _read_file(self, relative_path):
        if not isinstance(relative_path, str):
            raise PlanExecutionError("Invalid inspection path.")

        target = self.context._safe_path(relative_path)
        if target is None:
            raise PlanExecutionError(
                f"Unsafe inspection path: {relative_path}"
            )

        resolved = target.resolve(strict=True)
        resolved.relative_to(self.root)

        if target.is_symlink() or not resolved.is_file():
            raise PlanExecutionError(
                "Inspection target is not a regular file."
            )

        raw = resolved.read_bytes()
        if len(raw) > self.MAX_FILE_BYTES:
            raise PlanExecutionError("Inspection file exceeds 64 KB.")

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PlanExecutionError(
                "Inspection file is not valid UTF-8."
            ) from None

        preview = content[:self.PREVIEW_CHARS]
        return {
            "path": relative_path,
            "bytes": len(raw),
            "lines": len(content.splitlines()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "preview": preview,
            "truncated": len(content) > self.PREVIEW_CHARS,
        }

    def _execute_step(self, step):
        step_type = step["type"]

        # Modification is never permitted by plan approval alone.
        if step_type == "modify":
            raise PlanExecutionError(
                "Modification requires separate explicit approval."
            )

        if step_type in ("inspect", "review"):
            return {
                "files": [
                    self._read_file(path)
                    for path in step["files"]
                ],
                "description": step["description"],
            }

        if step_type == "analyze":
            scan = self.scanner.scan()
            results = []

            for path in step["files"]:
                module = (
                    path[:-3].replace("/", ".")
                    if path.endswith(".py")
                    else path.replace("/", ".")
                )

                if module in scan["modules"]:
                    results.append(
                        self.scanner.analyze_impact(module, scan)
                    )

            return {
                "scan_success": scan["success"],
                "module_count": len(scan["modules"]),
                "errors": scan["errors"][:20],
                "impact": results,
            }

        if step_type == "test":
            results = []

            for test_name in step["tests"]:
                self.engine.policy.validate_test(test_name)
                result = self.engine.run_registered_tests(test_name)

                results.append({
                    "test": test_name,
                    "success": bool(result.get("success")),
                    "returncode": result.get("returncode"),
                    "stdout": str(result.get("stdout", ""))[:4000],
                    "stderr": str(result.get("stderr", ""))[:4000],
                })

            return {
                "success": all(item["success"] for item in results),
                "tests": results,
            }

        raise PlanExecutionError("Unsupported plan step type.")

    def execute(self, plan_id):
        record = self.manager.get_plan(plan_id)

        if record.get("status") != "approved":
            raise PlanExecutionError(
                "Plan must be approved before execution."
            )

        task_id = record.get("task_id")

        if type(task_id) is not int or task_id < 1:
            raise PlanExecutionError("Invalid plan task ownership.")

        task = self.engine.get_task(task_id)

        if task is None or task.get("id") != task_id:
            raise PlanExecutionError("Plan task does not exist.")

        plan = record.get("plan")

        if record.get("plan_hash") != self.manager._digest(plan):
            raise PlanExecutionError("Plan hash verification failed.")

        # Revalidate the complete plan against fresh task context.
        validated = self._fresh_validate(record, task)
        steps = validated["steps"]

        if len(steps) > self.MAX_STEPS:
            raise PlanExecutionError(
                "Plan exceeds the 5-step execution limit."
            )

        # Reject the entire plan before claiming it. No partial execution.
        if any(step["type"] == "modify" for step in steps):
            raise PlanExecutionError(
                "Plan contains a modification step. "
                "Separate explicit approval is required; nothing executed."
            )

        # Validate every test suite before claiming the plan.
        for step in steps:
            if step["type"] == "test":
                for test_name in step["tests"]:
                    self.engine.policy.validate_test(test_name)

        # Claim only after every preflight check has passed.
        self._claim(record)

        results = []
        state = "failed"

        try:
            self._audit("PLAN_EXECUTION_STARTED", {
                "plan_id": plan_id,
                "task_id": task_id,
                "plan_hash": record["plan_hash"],
            })

            state = "completed"

            for step in steps:
                detail = {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "step_id": step["id"],
                    "step_type": step["type"],
                }

                try:
                    output = self._execute_step(step)
                    success = not (
                        isinstance(output, dict)
                        and output.get("success") is False
                    )

                    results.append({
                        "step_id": step["id"],
                        "type": step["type"],
                        "success": success,
                        "output": output,
                    })

                    if not success:
                        state = "failed"
                        self._audit("PLAN_STEP_FAILED", {
                            **detail,
                            "error": "Registered test suite failed",
                        })
                        break

                    self._audit("PLAN_STEP_COMPLETED", detail)

                except Exception as error:
                    state = "failed"

                    self._audit("PLAN_STEP_FAILED", {
                        **detail,
                        "error": type(error).__name__,
                    })

                    results.append({
                        "step_id": step["id"],
                        "type": step["type"],
                        "success": False,
                        "error": type(error).__name__,
                    })
                    break

        finally:
            record["execution"] = {
                "state": state,
                "started_at": record["execution"]["started_at"],
                "finished_at": self.manager._now(),
            }

            self.manager._write(
                self.manager._path(plan_id),
                record,
            )

            # Synchronize task status after the terminal plan record is saved.
            self.engine.update_task_status(
                task_id,
                "completed" if state == "completed" else "failed",
            )
            self._audit("PLAN_EXECUTION_FINISHED", {
                "plan_id": plan_id,
                "task_id": task_id,
                "state": state,
            })

        return {
            "plan_id": plan_id,
            "task_id": task_id,
            "state": state,
            "execution_performed": True,
            "steps": results,
        }
