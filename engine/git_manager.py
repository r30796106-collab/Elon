"""Safe, read-only Git repository inspection."""

import os
import shutil
import subprocess
from pathlib import Path


class GitOperationError(RuntimeError):
    """Raised when a bounded Git inspection cannot be completed."""


class GitManager:
    MAX_OUTPUT_CHARS = 100_000

    def __init__(self, project_root, timeout=10):
        self.root = Path(project_root).resolve()
        self.timeout = timeout
        if not self.root.is_dir():
            raise GitOperationError("Project root does not exist.")

    def _run(self, arguments):
        git = shutil.which("git")
        if not git:
            raise GitOperationError("Git executable is unavailable.")

        try:
            result = subprocess.run(
                [git, "--no-pager", *arguments],
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                shell=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", str(self.root)),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_PAGER": "cat",
                    "PAGER": "cat",
                    "GIT_OPTIONAL_LOCKS": "0",
                },
            )
        except subprocess.TimeoutExpired:
            raise GitOperationError("Git inspection timed out.") from None
        except OSError as error:
            raise GitOperationError(
                f"Git inspection failed: {type(error).__name__}"
            ) from None

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Git command failed.")
            raise GitOperationError(message.strip()[-2000:])

        return result.stdout[:self.MAX_OUTPUT_CHARS]

    def _verify_repository(self):
        top = self._run(["rev-parse", "--show-toplevel"]).strip()
        if Path(top).resolve() != self.root:
            raise GitOperationError(
                "Git repository root does not match the configured project root."
            )

    def status(self):
        self._verify_repository()
        output = self._run(["status", "--porcelain=v1", "--branch"])
        lines = output.splitlines()
        branch = "unknown"
        changes = []

        for line in lines:
            if line.startswith("## "):
                branch = line[3:]
                continue
            if len(line) >= 3:
                changes.append({
                    "index_status": line[0],
                    "worktree_status": line[1],
                    "path": line[3:],
                })

        return {
            "success": True,
            "read_only": True,
            "root": str(self.root),
            "branch": branch,
            "clean": not changes,
            "change_count": len(changes),
            "changes": changes,
        }

    def diff(self, staged=False, stat_only=True):
        self._verify_repository()
        command = ["diff", "--no-ext-diff", "--no-color"]
        if staged:
            command.append("--cached")
        if stat_only:
            command.append("--stat")
        else:
            command.extend(["--no-renames", "--"])
        output = self._run(command)
        return {
            "success": True,
            "read_only": True,
            "staged": bool(staged),
            "stat_only": bool(stat_only),
            "output": output,
            "truncated": len(output) >= self.MAX_OUTPUT_CHARS,
        }

    def log(self, limit=10):
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("Commit limit must be an integer.")
        if not 1 <= limit <= 50:
            raise ValueError("Commit limit must be between 1 and 50.")

        self._verify_repository()
        output = self._run([
            "log",
            "--oneline",
            "--decorate",
            "--no-color",
            "-n",
            str(limit),
        ])
        return {
            "success": True,
            "read_only": True,
            "limit": limit,
            "commits": output.splitlines(),
        }
