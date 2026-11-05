import hashlib
import os
import tempfile
from pathlib import Path

from providers.gemini_provider import GeminiProvider as OpenAIProvider, ProviderError
from security.policy import PolicyError


class SelfRepairEngine:
    MAX_PATCH_BYTES = 2_000_000
    MAX_CONTEXT_BYTES = 120_000

    def __init__(self, root, provider=None):
        self.root = Path(root).resolve()
        self.home = Path.home().resolve()
        self.provider = provider or OpenAIProvider()

    def _collect_context(self):
        excluded = {
            "backups", "logs", "__pycache__", ".git",
            "node_modules", ".venv", "venv"
        }

        candidates = []

        for path in self.root.rglob("*.py"):
            try:
                relative = path.relative_to(self.root)
                if any(part in excluded for part in relative.parts):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                candidates.append(path)
            except (OSError, ValueError):
                continue

        # Tests and core implementation are most useful first.
        candidates.sort(
            key=lambda p: (
                0 if "tests" in p.parts else
                1 if p.name in ("task_engine.py", "self_repair.py") else
                2,
                str(p)
            )
        )

        result = {}
        total = 0

        for path in candidates:
            try:
                raw = path.read_bytes()
                if len(raw) > 30000:
                    continue

                content = raw.decode("utf-8")
                relative = str(path.relative_to(self.home))

                if total + len(raw) > self.MAX_CONTEXT_BYTES:
                    break

                result[relative] = content
                total += len(raw)

            except (OSError, UnicodeDecodeError, ValueError):
                continue

        return result

    def _rollback(self, engine, applied):
        errors = []

        for item in reversed(applied):
            try:
                target = engine.policy.validate_modifiable_file(item["path"])

                current_hash = hashlib.sha256(
                    target.read_bytes()
                ).hexdigest()

                # Never overwrite a file changed by something else.
                if current_hash != item["new_hash"]:
                    raise PolicyError(
                        "Rollback skipped: file changed after ELON's patch"
                    )

                backup = Path(item["backup"]).resolve(strict=True)
                backup.relative_to(engine.backups.resolve())

                if backup.is_symlink() or not backup.is_file():
                    raise PolicyError("Invalid backup file")

                original = backup.read_bytes()
                backup_hash = hashlib.sha256(original).hexdigest()

                if backup_hash != item["old_hash"]:
                    raise PolicyError("Backup integrity check failed")

                temporary = None

                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=target.parent,
                        prefix=".elon_rollback_",
                        delete=False
                    ) as file:
                        temporary = Path(file.name)
                        file.write(original)

                    os.chmod(temporary, target.stat().st_mode)
                    os.replace(temporary, target)

                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()

                engine.audit.append(
                    "PATCH_ROLLED_BACK",
                    {
                        "path": item["path"],
                        "restored_hash": backup_hash
                    }
                )

            except Exception as error:
                errors.append(str(error))
                engine.audit.append(
                    "PATCH_ROLLBACK_FAILED",
                    {
                        "path": item["path"],
                        "error": str(error)
                    }
                )

        return errors

    def repair_task(self, engine, task_id, test_name="core"):
        """Disable legacy direct repair; use the controlled proposal workflow."""
        task = engine.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        if task["status"] != "pending":
            raise ValueError("Only pending tasks can be repaired")

        raise RuntimeError(
            "Direct repair is disabled. Use the controlled proposal, "
            "approval, and apply workflow through the ELON CLI."
        )

    def repair_task_multi_file(self, engine, task_id, test_name="core"):
        """Disable legacy multi-file direct repair."""
        task = engine.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        if task["status"] != "pending":
            raise ValueError("Only pending tasks can be repaired")

        raise RuntimeError(
            "Direct multi-file repair is disabled. Use the controlled "
            "proposal, approval, and apply workflow through the ELON CLI."
        )
