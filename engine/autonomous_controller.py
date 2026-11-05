from pathlib import Path

from core.self_repair import SelfRepairEngine
from engine.patch_transaction import PatchTransaction
from providers.gemini_provider import ProviderError


class AutonomousController:
    MAX_PLAN_BYTES = 100 * 1024

    def __init__(self, root, provider=None):
        self.root = Path(root).resolve()
        self.repair_engine = SelfRepairEngine(self.root, provider)
        self.provider = self.repair_engine.provider

    def _load_plan(self, engine, task_id):
        workspace = engine.policy.workspace
        plan = workspace / f"AI_PLAN_{task_id}.md"

        if plan.is_symlink():
            raise ValueError("Plan symlinks are not permitted")

        try:
            resolved = plan.resolve(strict=True)
            resolved.relative_to(workspace)
        except (OSError, ValueError):
            raise ValueError(
                f"Implementation plan missing: {plan.name}. "
                "Use prepare/ingest first."
            )

        if not resolved.is_file():
            raise ValueError("Plan must be a regular file")

        if resolved.stat().st_size > self.MAX_PLAN_BYTES:
            raise ValueError("Plan exceeds 100 KB")

        content = resolved.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError("Implementation plan is empty")

        return content

    def _extract_allowed_paths(self, plan):
        lines = plan.splitlines()
        in_section = False
        allowed = set()

        for line in lines:
            stripped = line.strip()

            if stripped.lower() == "## allowed file":
                if in_section:
                    raise ValueError("Duplicate Allowed file section")
                in_section = True
                continue

            if in_section and stripped.startswith("#"):
                break

            if not in_section or not stripped:
                continue

            if not stripped.startswith("- "):
                raise ValueError("Invalid entry in Allowed file section")

            raw_path = stripped[2:].strip().strip("`").strip()

            if not raw_path or "\\" in raw_path:
                raise ValueError("Invalid allowed file path")

            from pathlib import PurePosixPath
            candidate = PurePosixPath(raw_path)

            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("Unsafe allowed file path")

            normalized = candidate.as_posix()

            if normalized in ("", "."):
                raise ValueError("Invalid allowed file path")

            allowed.add(normalized)

        if not in_section or not allowed:
            raise ValueError("Plan must contain an Allowed file section")

        return allowed

    def _collect_safe_context(self, engine, plan):
        allowed_paths = self._extract_allowed_paths(plan)
        collected = self.repair_engine._collect_context()
        safe = {}
        found = set()

        project_parts = self.root.relative_to(Path.home().resolve()).parts

        protected_files = {
            "main.py",
            "core/audit.py",
            "core/self_repair.py",
            "engine/task_engine.py",
            "engine/patch_transaction.py",
            "engine/autonomous_controller.py",
            "providers/gemini_provider.py",
        }

        for path, content in collected.items():
            parts = Path(path).parts

            if parts[:len(project_parts)] != project_parts:
                continue

            relative_parts = parts[len(project_parts):]
            relative_path = Path(*relative_parts).as_posix()

            if relative_path not in allowed_paths:
                continue

            if (
                not relative_parts
                or "tests" in relative_parts
                or any(part.startswith("test_") for part in relative_parts)
                or relative_path in protected_files
            ):
                raise ValueError("Allowed file conflicts with protected paths")

            engine.policy.validate_modifiable_file(path)
            safe[relative_path] = content
            found.add(relative_path)

        if found != allowed_paths:
            missing = sorted(allowed_paths - found)
            raise ValueError("Allowed files missing or unsafe: " + ", ".join(missing))

        return safe

    def develop_task(self, engine, task_id, test_name="core"):
        task = engine.get_task(task_id)

        if not task:
            raise ValueError("Task not found")

        if task["status"] != "pending":
            raise ValueError("Only pending tasks can be developed")

        if not self.provider.configured():
            raise ProviderError("Gemini API is not configured")

        plan = self._load_plan(engine, task_id)

        # Do not build on top of an already failing project.
        baseline = engine.run_registered_tests("core")

        if not baseline["success"]:
            return {
                "success": False,
                "message": "Baseline tests failed. Repair existing failures first.",
                "attempts": 0,
                "test_result": baseline,
            }

        remaining = (
            engine.policy.MAX_REPAIRS - task["repair_attempts"]
        )

        if remaining <= 0:
            return {
                "success": False,
                "message": "Maximum development attempts reached.",
                "attempts": 0,
            }

        transaction = PatchTransaction(engine)
        result = baseline
        attempts = 0

        for attempt in range(remaining):
            applied = []

            try:
                source_files = self._collect_safe_context(engine, plan)

                proposal = self.provider.propose_development_patch_set(
                    task,
                    plan,
                    test_name,
                    result,
                    source_files,
                )

                if not isinstance(proposal, dict):
                    raise ProviderError("Invalid development proposal")

                action = proposal.get("action")

                if action == "no_patch":
                    raise ProviderError(
                        str(proposal.get("summary", "Gemini declined development"))
                    )

                if action != "patch_set":
                    raise ProviderError("Unknown development action")

                patches = proposal.get("patches")

                if not isinstance(patches, list) or not 1 <= len(patches) <= 8:
                    raise ProviderError("Development requires 1 to 8 patches")

                for patch in patches:
                    if not isinstance(patch, dict):
                        raise ProviderError("Invalid patch entry")

                    path = patch.get("path")
                    if not isinstance(path, str) or path not in source_files:
                        raise ProviderError(
                            "Patch path is outside supplied safe context"
                        )

                applied = transaction.apply(task_id, patches)
                attempts += 1

                engine.audit.append(
                    "AI_DEVELOPMENT_PATCH_SET",
                    {
                        "task_id": task_id,
                        "attempt": attempt + 1,
                        "files": [item["path"] for item in applied],
                        "summary": str(proposal.get("summary", ""))[:500],
                        "model": getattr(self.provider, "model", "custom"),
                    },
                )

                result = engine.run_registered_tests(test_name)

                if result["success"]:
                    try:
                        engine.complete_task(task_id)
                    except Exception as error:
                        return {
                            "success": False,
                            "message": (
                                "Tests passed, but task completion failed. "
                                "Changes were retained for inspection: " + str(error)
                            ),
                            "attempts": attempts,
                            "test_result": result,
                        }

                    # From this point onward, never roll back tested code.
                    applied = []

                    try:
                        engine.audit.append(
                            "AUTONOMOUS_DEVELOPMENT_SUCCEEDED",
                            {
                                "task_id": task_id,
                                "attempts": attempts,
                                "test": test_name,
                            },
                        )
                    except Exception as error:
                        return {
                            "success": True,
                            "message": (
                                "Development completed and tests passed. "
                                "Success audit warning: " + str(error)
                            ),
                            "attempts": attempts,
                            "test_result": result,
                        }

                    return {
                        "success": True,
                        "message": "Development completed; registered tests passed.",
                        "attempts": attempts,
                        "test_result": result,
                    }

                rollback_errors = transaction.rollback(applied)
                applied = []

                if rollback_errors:
                    return {
                        "success": False,
                        "message": "Tests failed and rollback was incomplete.",
                        "attempts": attempts,
                        "rollback_errors": rollback_errors,
                        "test_result": result,
                    }

            except Exception as error:
                rollback_errors = []

                if applied:
                    rollback_errors = transaction.rollback(applied)
                    applied = []

                try:
                    engine.audit.append(
                        "AUTONOMOUS_DEVELOPMENT_ATTEMPT_FAILED",
                        {
                            "task_id": task_id,
                            "attempt": attempt + 1,
                            "error": str(error)[:1000],
                        },
                    )
                except Exception:
                    pass

                if rollback_errors:
                    return {
                        "success": False,
                        "message": "Development failed; rollback incomplete.",
                        "attempts": attempts,
                        "rollback_errors": rollback_errors,
                    }

                if isinstance(error, ProviderError):
                    break

        engine.audit.append(
            "AUTONOMOUS_DEVELOPMENT_FAILED",
            {"task_id": task_id, "attempts": attempts},
        )

        return {
            "success": False,
            "message": "Development could not pass registered tests.",
            "attempts": attempts,
            "test_result": result,
        }
