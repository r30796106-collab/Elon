"""Safely generate ELON patch proposals using Aider in a disposable workspace."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from engine.patch_approval import PatchApprovalManager


class AiderProposalError(RuntimeError):
    pass


class AiderProposalService:
    MAX_FILES = 8
    MAX_SOURCE_BYTES = 2_000_000
    MAX_OUTPUT_CHARS = 20_000
    TIMEOUT_SECONDS = 180

    def __init__(
        self,
        engine,
        model="openrouter/cohere/north-mini-code:free",
        aider_path=None,
        environ=None,
    ):
        self.engine = engine
        self.root = Path(engine.root).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.model = model
        self.aider_path = aider_path or shutil.which("aider")
        self.environ = dict(os.environ if environ is None else environ)
        self.manager = PatchApprovalManager(engine)

    def _validate_task(self, task_id):
        task = self.engine.get_task(task_id)
        if not task:
            raise AiderProposalError("Task does not exist")
        if task["status"] != "pending":
            raise AiderProposalError("Only pending tasks can receive proposals")
        if task["repair_attempts"] >= self.engine.policy.MAX_REPAIRS:
            raise AiderProposalError("Maximum repair attempts reached")
        return task

    def _collect_sources(self, relative_paths):
        if not isinstance(relative_paths, (list, tuple)):
            raise AiderProposalError("File paths must be a list")
        if not 1 <= len(relative_paths) <= self.MAX_FILES:
            raise AiderProposalError("Select between 1 and 8 workspace files")

        sources = {}
        for relative in relative_paths:
            if not isinstance(relative, str) or not relative:
                raise AiderProposalError("Invalid workspace file path")

            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise AiderProposalError("Path traversal blocked")
            if relative in sources:
                raise AiderProposalError("Duplicate file path")

            try:
                target = self.engine.policy.validate_workspace_file(relative)
                target = target.resolve(strict=True)
                target.relative_to(self.workspace)
                if target.is_symlink() or not target.is_file():
                    raise ValueError("Not a regular file")
                raw = target.read_bytes()
                if len(raw) > self.MAX_SOURCE_BYTES:
                    raise ValueError("Source file exceeds 2 MB")
                content = raw.decode("utf-8")
            except Exception as error:
                raise AiderProposalError(
                    f"Workspace file rejected: {relative}: {error}"
                ) from error

            sources[relative] = {
                "path": target,
                "content": content,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        return sources

    def generate_proposal(
        self,
        task_id,
        relative_paths,
        instruction,
        test_name="core",
    ):
        self._validate_task(task_id)

        if not isinstance(instruction, str) or not instruction.strip():
            raise AiderProposalError("A coding instruction is required")
        if len(instruction) > 4000:
            raise AiderProposalError("Instruction exceeds 4000 characters")
        if not isinstance(self.model, str) or not self.model.strip():
            raise AiderProposalError("Aider model is not configured")
        if not self.aider_path:
            raise AiderProposalError("Aider executable was not found")
        api_key = self.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise AiderProposalError("OPENROUTER_API_KEY is not configured")

        self.engine.policy.validate_test(test_name)
        sources = self._collect_sources(relative_paths)

        with tempfile.TemporaryDirectory(prefix="elon-aider-") as temporary:
            temp_root = Path(temporary).resolve()

            for relative, source in sources.items():
                candidate = temp_root / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(source["content"], encoding="utf-8")

            prompt = (
                "You are editing a disposable copy of an ELON project workspace.\n"
                "Only modify the explicitly supplied files. Do not create files, "
                "delete files, or change file names. Do not run shell commands. "
                "Make the smallest correct change for this request.\n\n"
                "TASK:\n" + instruction.strip()
            )

            command = [
                self.aider_path,
                "--no-git",
                "--no-gitignore",
                "--yes-always",
                "--no-stream",
                "--no-auto-commits",
                "--model",
                self.model,
                "--message",
                prompt,
                *relative_paths,
            ]

            isolated_env = {
                "PATH": self.environ.get("PATH", ""),
                "HOME": str(temp_root),
                "TMPDIR": str(temp_root),
                "XDG_CONFIG_HOME": str(temp_root / ".config"),
                "OPENROUTER_API_KEY": api_key,
            }

            try:
                with tempfile.TemporaryFile(mode="w+b") as stdout_file, \
                     tempfile.TemporaryFile(mode="w+b") as stderr_file:
                    result = subprocess.run(
                        command,
                        cwd=str(temp_root),
                        env=isolated_env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        timeout=self.TIMEOUT_SECONDS,
                        check=False,
                        shell=False,
                    )

                    def read_tail(stream):
                        stream.seek(0, 2)
                        size = stream.tell()
                        stream.seek(max(0, size - self.MAX_OUTPUT_CHARS))
                        return stream.read().decode("utf-8", errors="replace")

                    output = (
                        read_tail(stdout_file) + "\n" +
                        read_tail(stderr_file)
                    )[-self.MAX_OUTPUT_CHARS:]

            except subprocess.TimeoutExpired as error:
                raise AiderProposalError("Aider timed out after 180 seconds") from error
            except OSError as error:
                raise AiderProposalError(f"Could not start Aider: {error}") from error

            if result.returncode != 0:
                raise AiderProposalError(
                    f"Aider failed with exit code {result.returncode}:\n{output}"
                )

            allowed = set(relative_paths)
            for item in temp_root.rglob("*"):
                if item.is_symlink():
                    raise AiderProposalError("Aider created a symbolic link")
                if item.is_file():
                    relative = item.relative_to(temp_root).as_posix()
                    allowed_metadata = {
                        ".aider.chat.history.md",
                        ".aider.input.history",
                        ".aider.tags.cache.v3",
                    }
                    if relative not in allowed and relative not in allowed_metadata:
                        raise AiderProposalError(
                            f"Aider created an unexpected file: {relative}"
                        )

            patches = []
            for relative, source in sources.items():
                candidate = temp_root / relative
                if not candidate.is_file() or candidate.is_symlink():
                    raise AiderProposalError(
                        f"Aider removed or replaced an allowed file: {relative}"
                    )

                content = candidate.read_text(encoding="utf-8")
                if content != source["content"]:
                    patches.append({
                        "path": "workspace/" + relative,
                        "content": content,
                        "expected_sha256": source["sha256"],
                    })

            if not patches:
                return {
                    "success": True,
                    "proposal": None,
                    "message": "Aider made no changes; no proposal was created.",
                    "output": output,
                }

            proposal = self.manager.save_proposal(
                task_id,
                patches,
                summary="Aider proposal: " + instruction.strip()[:1800],
                test_name=test_name,
            )

            return {
                "success": True,
                "proposal": proposal,
                "message": "Pending proposal created. Source files were not modified.",
                "output": output,
            }
