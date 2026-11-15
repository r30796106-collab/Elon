"""Deterministic, read-only task planning without external AI providers."""

from pathlib import Path

from engine.ai_task_planner import AITaskPlanner, TaskPlanError
from engine.project_context import ProjectContextBuilder


class DeterministicTaskPlanner:
    """Build bounded inspection, analysis, and registered-test plans."""

    MAX_CONTEXT_FILES = 12

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.context_builder = ProjectContextBuilder(self.root)
        # Reuse the strict plan validator only; no provider is configured or called.
        self.validator = AITaskPlanner(self.root, provider=None)

    def plan(self, task):
        clean_task = self.validator._validate_task(task)
        context = self.context_builder.build(clean_task)

        if not context.get("success"):
            raise TaskPlanError("Could not build project context.")

        files = [
            item["path"]
            for item in context.get("files", [])
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
        ][:self.MAX_CONTEXT_FILES]

        python_files = [
            path for path in files
            if path.endswith(".py")
        ]

        steps = []
        next_id = 1

        if files:
            steps.append({
                "id": next_id,
                "description": "Inspect the task-relevant project files.",
                "type": "inspect",
                "files": files,
                "tests": [],
            })
            next_id += 1

        if python_files:
            steps.append({
                "id": next_id,
                "description": "Analyze Python module dependencies and impact.",
                "type": "analyze",
                "files": python_files,
                "tests": [],
            })
            next_id += 1

        registered = self.validator.policy.REGISTERED_TESTS
        if "full" in registered:
            test_name = "full"
        elif "core" in registered:
            test_name = "core"
        elif registered:
            test_name = sorted(registered)[0]
        else:
            raise TaskPlanError("No registered test suite is available.")

        steps.append({
            "id": next_id,
            "description": "Run the registered full verification suite.",
            "type": "test",
            "files": [],
            "tests": [test_name],
        })

        proposal = {
            "action": "plan",
            "objective": "Read-only review: " + clean_task["title"],
            "requirements": [
                "Inspect task-relevant project context.",
                "Analyze available Python modules.",
                "Run a registered test suite.",
            ],
            "steps": steps,
            "risks": [
                "This deterministic plan does not modify project files.",
            ],
            "requires_approval": False,
        }

        return self.validator.validate_plan(proposal, context)
