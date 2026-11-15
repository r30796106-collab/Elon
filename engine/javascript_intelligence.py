"""Read-only JavaScript project scanning using Node's parser only."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path


class JavaScriptScanner:
    """Discover, syntax-check, and map local JavaScript imports without execution."""

    MAX_FILES = 500
    MAX_FILE_BYTES = 256 * 1024
    CHECK_TIMEOUT_SECONDS = 3
    MAX_TOTAL_CHECK_SECONDS = 10
    EXTENSIONS = {".js", ".mjs", ".cjs"}
    SKIP_DIRS = {
        ".git", ".venv", "venv", "__pycache__", "node_modules",
        "backups", "logs", ".pytest_cache", ".mypy_cache",
        ".next", "dist", "build", "coverage",
    }
    SENSITIVE_PARTS = (
        "secret", "credential", "password", "token",
        "api_key", "private_key",
    )
    IMPORT_PATTERNS = (
        re.compile(
            r"""\b(?:import|export)\s+(?:[^;"']*?\s+from\s+)?["']([^"']+)["']"""
        ),
        re.compile(r"""\brequire\s*\(\s*["']([^"']+)["']\s*\)"""),
        re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)"""),
    )

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()

    def _collect_files(self):
        candidates = []
        skipped = []

        for current, dirs, filenames in os.walk(
            self.root,
            followlinks=False,
        ):
            current_path = Path(current)
            dirs[:] = sorted(
                name for name in dirs
                if name not in self.SKIP_DIRS
                and not name.startswith(".")
                and not (current_path / name).is_symlink()
            )

            for filename in sorted(filenames):
                path = current_path / filename
                if path.suffix.lower() not in self.EXTENSIONS:
                    continue
                if path.is_symlink() or not path.is_file():
                    skipped.append(path.relative_to(self.root).as_posix())
                    continue
                lowered = filename.lower()
                if any(part in lowered for part in self.SENSITIVE_PARTS):
                    skipped.append(path.relative_to(self.root).as_posix())
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self.root)
                    size = resolved.stat().st_size
                except (OSError, ValueError):
                    skipped.append(path.relative_to(self.root).as_posix())
                    continue
                if size > self.MAX_FILE_BYTES:
                    skipped.append(path.relative_to(self.root).as_posix())
                    continue
                candidates.append(resolved)

                if len(candidates) >= self.MAX_FILES:
                    skipped.append("File limit reached")
                    return candidates, skipped

        return candidates, skipped

    def _resolve_local_import(self, source_path, specifier, known_paths):
        if not specifier.startswith("."):
            return None

        base = (source_path.parent / specifier).resolve()
        candidates = [base]

        if not base.suffix:
            candidates.extend(
                Path(str(base) + extension)
                for extension in (".js", ".mjs", ".cjs")
            )
            candidates.extend(
                base / ("index" + extension)
                for extension in (".js", ".mjs", ".cjs")
            )

        for candidate in candidates:
            try:
                candidate.relative_to(self.root)
            except ValueError:
                continue
            if candidate.is_symlink():
                continue
            if candidate in known_paths and candidate.is_file():
                return candidate
        return None

    def scan(self):
        files, skipped = self._collect_files()
        if not files:
            return {
                "status": "PASS",
                "node_available": shutil.which("node") is not None,
                "files_scanned": 0,
                "modules": {},
                "dependencies": {},
                "dependents": {},
                "syntax_errors": [],
                "skipped": skipped,
                "read_only": True,
                "executed_source": False,
                "message": "No JavaScript files found.",
            }

        node = shutil.which("node")
        if not node:
            return {
                "status": "WARN",
                "node_available": False,
                "files_scanned": 0,
                "modules": {},
                "dependencies": {},
                "dependents": {},
                "syntax_errors": [],
                "skipped": skipped,
                "read_only": True,
                "executed_source": False,
                "message": "JavaScript files found, but Node.js is unavailable for syntax checks.",
            }

        modules = {}
        dependencies = {}
        syntax_errors = []
        warnings = []
        known_paths = set(files)
        source_by_path = {}
        check_started = time.monotonic()

        for path in files:
            if (
                time.monotonic() - check_started
                >= self.MAX_TOTAL_CHECK_SECONDS
            ):
                warnings.append(
                    "JavaScript syntax-check time budget reached; remaining files skipped."
                )
                skipped.append("JavaScript syntax-check time budget reached")
                break

            relative = path.relative_to(self.root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                syntax_errors.append({
                    "path": relative,
                    "message": f"Could not read JavaScript file: {type(error).__name__}",
                    "language": "javascript",
                })
                continue

            source_by_path[path] = source
            module = Path(relative).with_suffix("").as_posix()
            modules[module] = {
                "path": relative,
                "bytes": path.stat().st_size,
                "lines": len(source.splitlines()),
            }
            dependencies[module] = set()

            try:
                result = subprocess.run(
                    [node, "--check", str(path)],
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.CHECK_TIMEOUT_SECONDS,
                    check=False,
                    shell=False,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                    },
                )
                if result.returncode != 0:
                    message = (result.stderr or result.stdout or "JavaScript syntax check failed")
                    syntax_errors.append({
                        "path": relative,
                        "message": message.strip()[-1000:],
                        "language": "javascript",
                    })
            except subprocess.TimeoutExpired:
                syntax_errors.append({
                    "path": relative,
                    "message": "JavaScript syntax check timed out.",
                    "language": "javascript",
                })
            except OSError as error:
                syntax_errors.append({
                    "path": relative,
                    "message": f"JavaScript syntax check failed: {type(error).__name__}",
                    "language": "javascript",
                })

        path_to_module = {
            Path(metadata["path"]).as_posix(): module
            for module, metadata in modules.items()
        }

        for path, source in source_by_path.items():
            relative = path.relative_to(self.root).as_posix()
            module = path_to_module.get(relative)
            if module is None:
                continue

            specifiers = []
            for pattern in self.IMPORT_PATTERNS:
                specifiers.extend(pattern.findall(source))

            for specifier in specifiers:
                dependency_path = self._resolve_local_import(
                    path,
                    specifier,
                    known_paths,
                )
                if dependency_path is None:
                    continue
                dependency_relative = dependency_path.relative_to(
                    self.root
                ).as_posix()
                dependency_module = path_to_module.get(dependency_relative)
                if dependency_module and dependency_module != module:
                    dependencies[module].add(dependency_module)

        dependencies = {
            module: sorted(items)
            for module, items in sorted(dependencies.items())
        }
        dependents = {module: set() for module in modules}
        for source, targets in dependencies.items():
            for target in targets:
                dependents[target].add(source)

        return {
            "status": (
                "FAIL" if syntax_errors
                else "WARN" if warnings
                else "PASS"
            ),
            "node_available": True,
            "files_scanned": len(modules),
            "modules": modules,
            "dependencies": dependencies,
            "dependents": {
                module: sorted(items)
                for module, items in sorted(dependents.items())
            },
            "syntax_errors": syntax_errors,
            "warnings": warnings,
            "skipped": skipped,
            "read_only": True,
            "executed_source": False,
            "message": (
                f"Scanned {len(modules)} JavaScript file(s); "
                f"{len(syntax_errors)} syntax/read error(s)."
            ),
        }
