"""Generate and validate non-executable AI development plans."""

import re

from engine.project_context import ProjectContextBuilder
from security.policy import SecurityPolicy


class TaskPlanError(ValueError):
    """Raised when an AI-generated task plan violates planner rules."""


class AITaskPlanner:
    ALLOWED_ACTION_FIELDS = {
        "action", "objective", "requirements",
        "steps", "risks", "requires_approval",
    }

    STEP_FIELDS = {
        "id", "description", "type", "files", "tests",
    }

    STEP_TYPES = {
        "inspect", "analyze", "modify", "test", "review",
    }

    RESTRICTED_TERMS = {
        "shell", "subprocess", "terminal command",
        "package installation", "pip install", "npm install",
        "apt install", "network request", "download",
        "credential", "password", "secret", "api key",
        "deployment", "deploy", "delete", "real-money",
        "private key",
    }

    def __init__(self, project_root, provider):
        self.root = project_root
        self.provider = provider
        self.context_builder = ProjectContextBuilder(project_root)
        self.policy = SecurityPolicy(project_root)

    def _contains_restricted_action(self, value):
        text = str(value).lower()
        return any(term in text for term in self.RESTRICTED_TERMS)

    def _validate_task(self, task):
        if not isinstance(task, dict):
            raise TaskPlanError("Task must be an object.")

        title = task.get("title")
        description = task.get("description", "")

        if not isinstance(title, str) or not title.strip():
            raise TaskPlanError("Task title is required.")

        if not isinstance(description, str):
            raise TaskPlanError("Task description must be text.")

        if len(title) > 200 or len(description) > 5000:
            raise TaskPlanError("Task description is too long.")

        return {
            "title": title.strip(),
            "description": description,
        }

    def validate_plan(self, plan, context):
        if not isinstance(plan, dict):
            raise TaskPlanError("Plan must be a JSON object.")

        if set(plan) != self.ALLOWED_ACTION_FIELDS:
            raise TaskPlanError("Invalid or unexpected plan fields.")

        if plan.get("action") != "plan":
            raise TaskPlanError("Unsupported planner action.")

        objective = plan.get("objective")
        requirements = plan.get("requirements")
        steps = plan.get("steps")
        risks = plan.get("risks")
        approval = plan.get("requires_approval")

        if not isinstance(objective, str) or not objective.strip():
            raise TaskPlanError("Plan objective is required.")

        if len(objective) > 1000:
            raise TaskPlanError("Plan objective is too long.")

        if self._contains_restricted_action(objective):
            raise TaskPlanError("Restricted operation found in plan objective.")

        if not isinstance(requirements, list) or len(requirements) > 30:
            raise TaskPlanError("Invalid requirements list.")

        if not all(
            isinstance(item, str) and item.strip() and len(item) <= 500
            for item in requirements
        ):
            raise TaskPlanError("Invalid requirement entry.")

        if any(self._contains_restricted_action(item) for item in requirements):
            raise TaskPlanError("Restricted operation found in requirements.")

        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise TaskPlanError("Plan must contain 1 to 20 steps.")

        if not isinstance(risks, list) or len(risks) > 20:
            raise TaskPlanError("Invalid risks list.")

        if not all(
            isinstance(item, str) and len(item) <= 500
            for item in risks
        ):
            raise TaskPlanError("Invalid risk entry.")

        if not isinstance(approval, bool):
            raise TaskPlanError("requires_approval must be boolean.")

        allowed_files = {
            item["path"]
            for item in context.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }

        registered_tests = set(self.policy.REGISTERED_TESTS)
        seen_ids = set()
        validated_steps = []
        approval_required = False

        for expected_id, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise TaskPlanError("Each step must be an object.")

            if set(step) != self.STEP_FIELDS:
                raise TaskPlanError("Invalid or unexpected step fields.")

            step_id = step.get("id")
            description = step.get("description")
            step_type = step.get("type")
            files = step.get("files")
            tests = step.get("tests")

            if (
                type(step_id) is not int
                or step_id != expected_id
                or step_id in seen_ids
            ):
                raise TaskPlanError("Step IDs must be sequential.")

            seen_ids.add(step_id)

            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > 1000
            ):
                raise TaskPlanError("Invalid step description.")

            if not isinstance(step_type, str) or step_type not in self.STEP_TYPES:
                raise TaskPlanError("Unsupported step type.")

            if not isinstance(files, list) or len(files) > 12:
                raise TaskPlanError("Invalid step files.")

            if not all(isinstance(path, str) and path.strip() for path in files):
                raise TaskPlanError("Every step file must be a non-empty path.")

            if not isinstance(tests, list) or len(tests) > 5:
                raise TaskPlanError("Invalid step tests.")

            if not all(isinstance(name, str) and name.strip() for name in tests):
                raise TaskPlanError("Every test name must be non-empty text.")

            if len(set(files)) != len(files):
                raise TaskPlanError("Duplicate file in step.")

            if len(set(tests)) != len(tests):
                raise TaskPlanError("Duplicate test in step.")

            # Only explicit test steps may execute registered suites.
            if step_type != "test" and tests:
                raise TaskPlanError(
                    "Only test steps may specify registered test suites."
                )

            if step_type == "test" and files:
                raise TaskPlanError(
                    "Test steps must not specify project files."
                )

            for path in files:
                if not isinstance(path, str) or path not in allowed_files:
                    raise TaskPlanError(
                        "Plan references a file outside project context."
                    )

                if self.context_builder._safe_path(path) is None:
                    raise TaskPlanError("Unsafe project file path.")

                if step_type == "modify":
                    try:
                        self.policy.validate_modifiable_file(path)
                    except (ValueError, OSError):
                        raise TaskPlanError(
                            "Modification targets a protected file."
                        ) from None

            for test_name in tests:
                if not isinstance(test_name, str):
                    raise TaskPlanError("Test name must be text.")

                if test_name not in registered_tests:
                    raise TaskPlanError("Unregistered test suite.")

            if step_type == "test" and not tests:
                raise TaskPlanError(
                    "Test steps must specify a registered test suite."
                )

            if step_type == "modify" and not files:
                raise TaskPlanError(
                    "Modification steps must specify contextual files."
                )

            if self._contains_restricted_action(description):
                raise TaskPlanError(
                    "Restricted operation found in plan."
                )

            validated_steps.append({
                "id": step_id,
                "description": description.strip(),
                "type": step_type,
                "files": list(files),
                "tests": list(tests),
            })

        risk_text = " ".join(risks).lower()
        approval_required = (
            approval
            or any(
                term in risk_text
                for term in self.RESTRICTED_TERMS
            )
        )

        return {
            "action": "plan",
            "objective": objective.strip(),
            "requirements": list(requirements),
            "steps": validated_steps,
            "risks": list(risks),
            "requires_approval": approval_required,
            "validated": True,
            "execution_performed": False,
        }

    def plan(self, task):
        clean_task = self._validate_task(task)

        context = self.context_builder.build(clean_task)

        if not context.get("success"):
            raise TaskPlanError("Could not build project context.")

        proposal = self.provider.propose_task_plan(
            clean_task,
            context,
        )

        return self.validate_plan(proposal, context)
