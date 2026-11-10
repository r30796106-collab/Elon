from pathlib import Path


class PolicyError(ValueError):
    pass


class ApprovalRequired(PolicyError):
    pass


class SecurityPolicy:
    MAX_REPAIRS = 5
    DOWNLOAD_APPROVAL_BYTES = 250 * 1024 * 1024

    REGISTERED_TESTS = {
        "core": ["tests.test_core", "tests.test_self_repair"],
        "demo": ["workspace.test_repair_demo"],
        "full": [
            "tests.test_ai_task_planner",
            "tests.test_ai_tool_controller",
            "tests.test_ai_tool_orchestrator",
            "tests.test_aider_proposal_service",
            "tests.test_autonomous_controller",
            "tests.test_core",
            "tests.test_error_diagnostics",
            "tests.test_gemini_development",
            "tests.test_health_check",
            "tests.test_local_bridge",
            "tests.test_package_policy",
            "tests.test_patch_approval",
            "tests.test_patch_process_crash",
            "tests.test_patch_proposal_service",
            "tests.test_patch_recovery",
            "tests.test_patch_recovery_crash",
            "tests.test_patch_transaction",
            "tests.test_patch_workflow_integration",
            "tests.test_plan_approval",
            "tests.test_plan_executor",
            "tests.test_project_context",
            "tests.test_project_intelligence",
            "tests.test_self_repair",
            "tests.test_tool_registry",
        ],
    }

    PACKAGE_MANAGERS = {
        "pkg", "apt", "pip", "pip3", "npm"
    }

    PROTECTED_DIRS = {
        ".ssh", ".gnupg", ".termux", ".config",
        ".android", "Android"
    }

    PROTECTED_FILES = {
        ".env", "id_rsa", "id_ed25519",
        "credentials.json", "secrets.json",
        "token.json"
    }

    PROTECTED_NAME_PARTS = (
        "secret", "credential", "password",
        "private_key", "api_key", "token"
    )

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.home_root = Path.home().resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.backups = self.root / "backups"

    def _check_relative_path(self, relative_path):
        path = Path(relative_path)

        if path.is_absolute() or ".." in path.parts:
            raise PolicyError("Path traversal blocked")

        if not path.parts:
            raise PolicyError("Empty path blocked")

        if any("\x00" in part for part in path.parts):
            raise PolicyError("Invalid path")

        return path

    def validate_modifiable_file(self, relative_path):
        path = self._check_relative_path(relative_path)

        # Backward compatibility: existing workspace files are
        # resolved first. Other relative paths can address Termux home.
        if path.parts[0] == "workspace":
            base = self.root
            target = base / path
        else:
            workspace_target = self.workspace / path

            if workspace_target.exists() or workspace_target.is_symlink():
                base = self.workspace
                target = workspace_target
            else:
                base = self.home_root
                target = base / path

        # Reject symlinks in every component.
        current = base
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise PolicyError("Symbolic links are not permitted")

        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(base)
        except (OSError, ValueError):
            raise PolicyError("Path is outside the permitted boundary")

        if not resolved.is_file():
            raise PolicyError("Target must be an existing regular file")

        parts_lower = {part.lower() for part in path.parts}
        if parts_lower & self.PROTECTED_DIRS:
            raise PolicyError("Protected directory cannot be modified")

        filename = resolved.name.lower()

        if filename in self.PROTECTED_FILES:
            raise PolicyError("Protected file cannot be modified")

        if filename.endswith((".pem", ".key", ".p12", ".pfx")):
            raise PolicyError("Private key files are protected")

        if any(word in filename for word in self.PROTECTED_NAME_PARTS):
            raise PolicyError("Potential credential file is protected")

        # Protect ELON's security and audit enforcement code.
        try:
            relative_to_elon = resolved.relative_to(self.root)
            elon_parts = relative_to_elon.parts
        except ValueError:
            elon_parts = ()

        if elon_parts:
            if elon_parts[0] in {"security", "backups", "logs"}:
                raise PolicyError("ELON security, backup and log files are protected")

            if elon_parts == ("core", "audit.py"):
                raise PolicyError("Audit implementation is protected")

            if elon_parts == ("engine", "task_engine.py"):
                raise PolicyError("Task engine requires explicit maintenance")

            if elon_parts == ("engine", "autonomous_controller.py"):
                raise PolicyError("Autonomous controller is permanently protected")

        return resolved

    def validate_workspace_file(self, relative_path):
        path = self._check_relative_path(relative_path)
        return self.validate_modifiable_file(
            Path("workspace") / path
        )

    def validate_test(self, test_name):
        if test_name not in self.REGISTERED_TESTS:
            raise PolicyError("Test is not registered")

        return self.REGISTERED_TESTS[test_name]

    def download_requires_approval(self, size_bytes):
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise PolicyError("Invalid download size")

        return size_bytes > self.DOWNLOAD_APPROVAL_BYTES

    def validate_package_manager(self, manager):
        if not isinstance(manager, str) or manager not in self.PACKAGE_MANAGERS:
            raise PolicyError("Unrecognized package manager")

        raise ApprovalRequired(
            "Package installation requires explicit user approval. "
            "No installation workflow is currently authorized."
        )

    def require_delete_approval(self, permanent=True, item_count=1):
        if permanent or item_count > 1:
            raise ApprovalRequired(
                "Deletion requires explicit user approval"
            )

        return True
