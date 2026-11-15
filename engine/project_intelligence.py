import ast
from collections import deque
from pathlib import Path


class ProjectScanner:
    MAX_FILES = 500
    MAX_FILE_BYTES = 256 * 1024

    ALLOWED_ROOTS = {
        "core", "engine", "providers", "security", "tests",
        "tools", "workspace", "src", "app", "lib",
        "frontend", "backend", "scripts", "server", "client",
        "test",
    }

    SKIP_DIRS = {
        ".git", "__pycache__", ".venv", "venv",
        "node_modules", "backups", "logs",
        ".pytest_cache", ".mypy_cache",
    }

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()

    def _module_name(self, path):
        relative = path.relative_to(self.root).with_suffix("")
        parts = list(relative.parts)

        if parts[-1] == "__init__":
            parts.pop()

        return ".".join(parts)

    def _candidate_modules(self, name):
        parts = name.split(".")

        while parts:
            candidate = ".".join(parts)
            yield candidate
            parts.pop()

    def _collect_files(self):
        candidates = []

        main_file = self.root / "main.py"
        if main_file.is_file() and not main_file.is_symlink():
            candidates.append(main_file)

        for folder in sorted(self.ALLOWED_ROOTS):
            base = self.root / folder

            if not base.is_dir() or base.is_symlink():
                continue

            for current, dirs, files in __import__("os").walk(
                base, followlinks=False
            ):
                current_path = Path(current)

                dirs[:] = sorted(
                    name for name in dirs
                    if name not in self.SKIP_DIRS
                    and not (current_path / name).is_symlink()
                )

                for filename in sorted(files):
                    path = current_path / filename

                    if filename.endswith(".py"):
                        if path.is_symlink() or not path.is_file():
                            continue
                        candidates.append(path)

        return sorted(set(candidates))

    def scan(self):
        files = self._collect_files()

        modules = {}
        errors = []
        skipped = []
        dependencies = {}
        imports_by_module = {}

        for path in files:
            if len(modules) >= self.MAX_FILES:
                skipped.append("File limit reached")
                break

            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.root)

                if path.is_symlink():
                    skipped.append(str(path.relative_to(self.root)))
                    continue

                size = resolved.stat().st_size
                if size > self.MAX_FILE_BYTES:
                    skipped.append(str(path.relative_to(self.root)))
                    continue

                source = resolved.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                module = self._module_name(path)

                modules[module] = {
                    "path": path.relative_to(self.root).as_posix(),
                    "lines": len(source.splitlines()),
                    "bytes": size,
                    "symbols": [],
                }

                dependencies[module] = set()
                imports_by_module[module] = []

                for node in ast.walk(tree):
                    imported = []

                    if isinstance(node, ast.Import):
                        imported.extend(alias.name for alias in node.names)

                    elif isinstance(node, ast.ImportFrom):
                        base = node.module or ""

                        if node.level:
                            package = module.split(".")[:-1]
                            if path.name == "__init__.py":
                                package = module.split(".")

                            remove = node.level - 1
                            if remove:
                                package = package[:-remove]

                            prefix = ".".join(package)
                            base = (
                                f"{prefix}.{base}"
                                if prefix and base else prefix or base
                            )

                        if base:
                            imported.append(base)

                        for alias in node.names:
                            if alias.name != "*":
                                imported.append(
                                    f"{base}.{alias.name}"
                                    if base else alias.name
                                )

                    imports_by_module[module].extend(imported)

                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    ):
                        modules[module]["symbols"].append({
                            "name": node.name,
                            "kind": (
                                "class" if isinstance(node, ast.ClassDef)
                                else "function"
                            ),
                            "line": node.lineno,
                        })

                modules[module]["symbols"].sort(
                    key=lambda item: (item["line"], item["name"])
                )

            except (OSError, UnicodeError, SyntaxError, ValueError) as error:
                error_record = {
                    "path": path.relative_to(self.root).as_posix(),
                    "error": type(error).__name__,
                    "message": str(error)[:500],
                }

                if isinstance(error, SyntaxError):
                    error_record["line"] = error.lineno
                    error_record["column"] = error.offset
                    error_record["source"] = (
                        error.text.strip()[:300]
                        if error.text else None
                    )

                errors.append(error_record)

        # Resolve stored import names after all project modules are known.
        known = set(modules)
        resolved_dependencies = {
            module: set() for module in modules
        }

        for module, names in imports_by_module.items():
            for name in names:
                for candidate in self._candidate_modules(name):
                    if candidate in known and candidate != module:
                        resolved_dependencies[module].add(candidate)
                        break

        dependencies = {
            name: sorted(items)
            for name, items in sorted(resolved_dependencies.items())
        }

        dependents = {name: set() for name in modules}

        for source, targets in dependencies.items():
            for target in targets:
                dependents[target].add(source)

        return {
            "success": True,
            "modules": modules,
            "dependencies": dependencies,
            "dependents": {
                name: sorted(items)
                for name, items in sorted(dependents.items())
            },
            "errors": errors,
            "skipped": skipped,
            "summary": {
                "files_found": len(files),
                "modules_scanned": len(modules),
                "dependency_edges": sum(
                    len(items) for items in dependencies.values()
                ),
                "syntax_errors": len(errors),
                "skipped_files": len(skipped),
            },
        }

    def analyze_impact(self, module, scan_result=None):
        result = scan_result or self.scan()

        if module.endswith(".py"):
            module = module[:-3].replace("/", ".")

        if module.endswith(".__init__"):
            module = module[:-9]

        if module not in result["modules"]:
            raise ValueError(f"Unknown project module: {module}")

        dependents = result["dependents"]
        direct = set(dependents.get(module, []))
        visited = set()
        queue = deque(direct)

        while queue:
            current = queue.popleft()

            if current in visited:
                continue

            visited.add(current)
            queue.extend(dependents.get(current, []))

        transitive = visited - direct

        return {
            "module": module,
            "direct_dependents": sorted(direct),
            "transitive_dependents": sorted(transitive),
        }
