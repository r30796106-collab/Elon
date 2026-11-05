import hashlib
from pathlib import Path

from core.self_repair import SelfRepairEngine
from engine.autonomous_controller import AutonomousController
from engine.patch_approval import PatchApprovalManager
from providers.gemini_provider import GeminiProvider, ProviderError


class PatchProposalService:
    """Generate reviewable proposals. Never applies source changes."""

    def __init__(self, engine, provider=None):
        self.engine = engine
        self.root = Path(engine.root).resolve()
        self.provider = provider or GeminiProvider()
        self.repairer = SelfRepairEngine(self.root, self.provider)
        self.controller = AutonomousController(self.root, self.provider)
        self.manager = PatchApprovalManager(engine)

    def _check_task(self, task_id):
        task = self.engine.get_task(task_id)
        if not task:
            raise ValueError("Task does not exist")
        if task["status"] != "pending":
            raise ValueError("Only pending tasks can receive proposals")
        if task["repair_attempts"] >= self.engine.policy.MAX_REPAIRS:
            raise ValueError("Maximum repair attempts reached")
        if not self.provider.configured():
            raise ProviderError("Gemini API is not configured")
        return task

    @staticmethod
    def _summary(proposal):
        value = proposal.get("summary", "")
        if not isinstance(value, str):
            raise ProviderError("Proposal summary must be text")
        return value[:2000]

    def generate_repair(self, task_id, test_name="core", multi_file=False):
        task = self._check_task(task_id)
        self.engine.policy.validate_test(test_name)

        baseline = self.engine.run_registered_tests(test_name)
        if baseline.get("success"):
            return {
                "success": True,
                "proposal": None,
                "message": "Tests already pass; no repair proposal needed.",
            }

        source_files = self.repairer._collect_context()
        if not source_files:
            raise ValueError("No safe source files available")

        if multi_file:
            proposal = self.provider.propose_patch_set(
                task, test_name, baseline, source_files
            )
            if not isinstance(proposal, dict):
                raise ProviderError("Invalid repair proposal")
            if proposal.get("action") == "no_patch":
                raise ProviderError(self._summary(proposal))
            if proposal.get("action") != "patch_set":
                raise ProviderError("Unknown repair proposal action")

            patches = proposal.get("patches")
            if not isinstance(patches, list) or not 2 <= len(patches) <= 8:
                raise ProviderError("Multi-file repair requires 2 to 8 patches")

            for patch in patches:
                if not isinstance(patch, dict):
                    raise ProviderError("Invalid patch entry")
                if patch.get("path") not in source_files:
                    raise ProviderError("Patch path is outside safe context")
        else:
            proposal = self.provider.propose_patch(
                task, test_name, baseline, source_files
            )
            if not isinstance(proposal, dict):
                raise ProviderError("Invalid repair proposal")
            if proposal.get("action") == "no_patch":
                raise ProviderError(self._summary(proposal))
            if proposal.get("action") != "patch":
                raise ProviderError("Unknown repair proposal action")

            path = proposal.get("path")
            content = proposal.get("content")
            if not isinstance(path, str) or path not in source_files:
                raise ProviderError("Patch path is outside safe context")
            if not isinstance(content, str):
                raise ProviderError("Patch content must be text")

            expected = hashlib.sha256(
                source_files[path].encode("utf-8")
            ).hexdigest()
            patches = [{
                "path": path,
                "expected_sha256": expected,
                "content": content,
            }]

        saved = self.manager.save_proposal(
            task_id,
            patches,
            summary=self._summary(proposal),
            test_name=test_name,
        )
        return {
            "success": True,
            "proposal": saved,
            "message": "Repair proposal saved for human review.",
        }

    def generate_development(self, task_id, test_name="core"):
        task = self._check_task(task_id)
        self.engine.policy.validate_test(test_name)

        plan = self.controller._load_plan(self.engine, task_id)
        baseline = self.engine.run_registered_tests("core")
        if not baseline.get("success"):
            return {
                "success": False,
                "proposal": None,
                "message": "Core baseline tests failed. Repair existing failures first.",
            }

        safe_context = self.controller._collect_safe_context(
            self.engine, plan
        )
        if not safe_context:
            raise ValueError("No allowed source files found in the plan")

        home_prefix = self.root.relative_to(
            Path.home().resolve()
        ).as_posix()

        # Use the policy's home-relative path convention for every proposal.
        source_files = {
            f"{home_prefix}/{path}": content
            for path, content in safe_context.items()
        }

        proposal = self.provider.propose_development_patch_set(
            task, plan, test_name, baseline, source_files
        )
        if not isinstance(proposal, dict):
            raise ProviderError("Invalid development proposal")
        if proposal.get("action") == "no_patch":
            raise ProviderError(self._summary(proposal))
        if proposal.get("action") != "patch_set":
            raise ProviderError("Unknown development proposal action")

        patches = proposal.get("patches")
        if not isinstance(patches, list) or not 1 <= len(patches) <= 8:
            raise ProviderError("Development requires 1 to 8 patches")

        for patch in patches:
            if not isinstance(patch, dict):
                raise ProviderError("Invalid development patch")
            if patch.get("path") not in source_files:
                raise ProviderError("Patch path is outside the plan's allowed files")

        saved = self.manager.save_proposal(
            task_id,
            patches,
            summary=self._summary(proposal),
            test_name=test_name,
        )
        return {
            "success": True,
            "proposal": saved,
            "message": "Development proposal saved for human review.",
        }
