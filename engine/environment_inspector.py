"""Read-only Python/Node environment and dependency inspection."""

import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


class EnvironmentInspector:
    MAX_MANIFEST_BYTES = 256 * 1024
    NODE_TIMEOUT_SECONDS = 3

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()

    @staticmethod
    def _package_name(requirement):
        if not isinstance(requirement, str):
            return None
        value = requirement.strip()
        if not value or value.startswith(("#", "-")):
            return None
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", value)
        return match.group(1) if match else None

    def _read_text(self, path):
        if path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > self.MAX_MANIFEST_BYTES:
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def _python_requirements(self):
        names = set()
        skipped = 0

        requirements = self.root / "requirements.txt"
        text = self._read_text(requirements)
        if text is not None:
            for line in text.splitlines():
                name = self._package_name(line)
                if name:
                    names.add(name)
                elif line.strip() and not line.lstrip().startswith("#"):
                    skipped += 1

        pyproject = self.root / "pyproject.toml"
        text = self._read_text(pyproject)
        if text is not None and tomllib is not None:
            try:
                data = tomllib.loads(text)
                project = data.get("project", {})
                for item in project.get("dependencies", []):
                    name = self._package_name(item)
                    if name:
                        names.add(name)
                    else:
                        skipped += 1
            except (ValueError, TypeError, AttributeError):
                skipped += 1

        installed = []
        missing = []
        for name in sorted(names):
            try:
                version = importlib.metadata.version(name)
                installed.append({"name": name, "version": version})
            except importlib.metadata.PackageNotFoundError:
                missing.append(name)

        return {
            "declared": sorted(names),
            "installed": installed,
            "missing": missing,
            "skipped_entries": skipped,
        }

    def _node_requirements(self):
        manifest = self.root / "package.json"
        text = self._read_text(manifest)
        if text is None:
            return {
                "manifest_found": False,
                "declared": [],
                "installed": [],
                "missing": [],
            }

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {
                "manifest_found": True,
                "declared": [],
                "installed": [],
                "missing": [],
                "error": "package.json is invalid JSON.",
            }

        names = set()
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            values = data.get(section, {})
            if isinstance(values, dict):
                names.update(
                    name for name in values
                    if isinstance(name, str) and name.strip()
                )

        modules = self.root / "node_modules"
        installed = []
        missing = []
        for name in sorted(names):
            candidate = modules / name
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.root)
                present = resolved.is_dir()
            except (OSError, ValueError):
                present = False

            if present:
                installed.append(name)
            else:
                missing.append(name)

        return {
            "manifest_found": True,
            "declared": sorted(names),
            "installed": installed,
            "missing": missing,
        }

    def _node_version(self):
        executable = shutil.which("node")
        if not executable:
            return {"available": False, "version": None}

        try:
            result = subprocess.run(
                [executable, "--version"],
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.NODE_TIMEOUT_SECONDS,
                check=False,
                shell=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"available": True, "version": None}

        if result.returncode != 0:
            return {"available": True, "version": None}
        return {
            "available": True,
            "version": (result.stdout or "").strip()[:100],
        }

    def inspect(self):
        python = {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "virtual_environment": sys.prefix != sys.base_prefix,
        }
        git = shutil.which("git")
        node = self._node_version()
        python_dependencies = self._python_requirements()
        node_dependencies = self._node_requirements()

        warnings = []
        if python_dependencies["missing"]:
            warnings.append(
                "Missing Python dependencies: "
                + ", ".join(python_dependencies["missing"])
            )
        if node_dependencies["missing"]:
            warnings.append(
                "Missing Node dependencies: "
                + ", ".join(node_dependencies["missing"])
            )
        if node_dependencies["manifest_found"] and not node["available"]:
            warnings.append(
                "package.json exists but Node.js is unavailable."
            )
        if node_dependencies.get("error"):
            warnings.append(node_dependencies["error"])

        return {
            "status": "WARN" if warnings else "PASS",
            "read_only": True,
            "network_used": False,
            "installation_performed": False,
            "package_installation": "BLOCKED; explicit approval required",
            "platform": platform.platform(),
            "python": python,
            "git": {"available": bool(git)},
            "node": node,
            "python_dependencies": python_dependencies,
            "node_dependencies": node_dependencies,
            "warnings": warnings,
        }
