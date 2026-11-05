import ast
import re
from pathlib import Path

from engine.project_intelligence import ProjectScanner


class ProjectContextBuilder:
    """Build bounded, read-only code context for a development task."""

    MAX_CONTEXT_FILES = 12
    MAX_FILE_BYTES = 64 * 1024
    MAX_TOTAL_BYTES = 256 * 1024

    EXCLUDED_PARTS = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "backups",
        "logs",
        "node_modules",
        ".config",
        ".ssh",
        ".gnupg",
    }

    EXCLUDED_DIRS = {
        "security",
    }

    EXCLUDED_NAME_PARTS = (
        "secret",
        "credential",
        "password",
        "token",
        "api_key",
        "private_key",
    )

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.scanner = ProjectScanner(self.root)

    def _safe_path(self, relative_path):
        relative = Path(relative_path)

        if relative.is_absolute() or not relative.parts:
            return None

        if any(
            part.startswith(".") or part in {".."}
            for part in relative.parts
        ):
            return None

        allowed = (
            relative.parts[0] in {
                "core", "engine", "providers", "tests",
                "security", "workspace",
            }
            or relative.as_posix() == "main.py"
        )

        if not allowed:
            return None

        if any(part.lower() in self.EXCLUDED_PARTS for part in relative.parts):
            return None

        if any(part.lower() in self.EXCLUDED_DIRS for part in relative.parts):
            return None

        filename = relative.name.lower()
        if any(word in filename for word in self.EXCLUDED_NAME_PARTS):
            return None

        if relative.suffix != ".py":
            return None

        path = self.root / relative
        current = self.root

        try:
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return None

            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)

            if not resolved.is_file():
                return None

            if resolved.stat().st_size > self.MAX_FILE_BYTES:
                return None

        except (OSError, ValueError):
            return None

        return resolved

    def _keywords(self, task):
        if isinstance(task, dict):
            text = " ".join(
                str(task.get(key, ""))
                for key in ("title", "description")
            )
        elif isinstance(task, str):
            text = task
        else:
            raise ValueError("Task must be text or a task object")

        if not text.strip():
            raise ValueError("Task description cannot be empty")

        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())

        ignored = {
            "the", "and", "for", "with", "from", "that",
            "this", "into", "make", "create", "update",
            "please", "should", "would", "could", "need",
            "want", "task", "project", "file", "files",
        }

        return sorted(set(word for word in words if word not in ignored))

    def _extract_symbols(self, source):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        symbols = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append({
                    "name": node.name,
                    "kind": (
                        "class" if isinstance(node, ast.ClassDef)
                        else "function"
                    ),
                    "line": node.lineno,
                })

        return sorted(symbols, key=lambda item: (item["line"], item["name"]))

    def _normalize_term(self, term):
        """Normalize common task-language variants for matching."""
        aliases = {
            "calculator": "calcul",
            "calculate": "calcul",
            "calculation": "calcul",
            "calculations": "calcul",
            "validation": "valid",
            "validate": "valid",
            "validated": "valid",
            "validating": "valid",
            "quantities": "quant",
            "quantity": "quant",
            "booleans": "bool",
            "boolean": "bool",
            "negative": "neg",
            "negatives": "neg",
            "strings": "string",
            "integers": "integer",
        }
        return aliases.get(term.lower(), term.lower())

    def _term_set(self, text):
        # Split snake_case identifiers so their individual words can match.
        text = text.replace("_", " ")
        words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower())
        return {self._normalize_term(word) for word in words}

    def _score_candidate(self, module, relative, source, keywords):
        """Score path, identifiers, source content and test relevance."""
        task_terms = {self._normalize_term(word) for word in keywords}
        path_terms = self._term_set(module + " " + relative)
        source_terms = self._term_set(source)

        try:
            symbols = self._extract_symbols(source)
        except (SyntaxError, ValueError):
            symbols = []

        symbol_terms = set()
        for symbol in symbols:
            symbol_terms.update(self._term_set(symbol["name"]))

        path_hits = task_terms & path_terms
        symbol_hits = task_terms & symbol_terms
        source_hits = task_terms & source_terms

        score = (
            len(path_hits) * 6
            + len(symbol_hits) * 5
            + min(len(source_hits), 8)
        )

        is_test = (
            relative.startswith("tests/")
            or relative.startswith("workspace/test_")
            or relative.startswith("workspace/tests/")
        )

        if is_test and score:
            score += 3
        elif score:
            score += 1

        matching_symbols = [
            symbol for symbol in symbols
            if self._term_set(symbol["name"]) & task_terms
        ]

        return {
            "module": module,
            "path": relative,
            "score": score,
            "symbols": matching_symbols[:30],
            "source": source,
            "is_test": is_test,
            "matched_terms": sorted(path_hits | symbol_hits | source_hits),
        }

    def build(self, task):
        keywords = self._keywords(task)
        scan = self.scanner.scan()
        warnings = []
        candidates = {}

        # First pass: score every safe, readable Python module.
        for module, metadata in scan["modules"].items():
            relative = metadata["path"]
            path = self._safe_path(relative)

            if path is None:
                continue

            try:
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    warnings.append(f"File too large: {relative}")
                    continue

                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                warnings.append(f"Could not read: {relative}")
                continue

            item = self._score_candidate(
                module, relative, source, keywords
            )

            if item["score"] > 0:
                candidates[module] = item

        # Second pass: reward actual dependencies of strongly relevant
        # implementation modules. Do not let unrelated tests drive this.
        dependency_scores = {}
        dependencies = scan.get("dependencies", {})

        for module, item in list(candidates.items()):
            if item["is_test"] or item["score"] < 6:
                continue

            for dependency in dependencies.get(module, []):
                # A useful helper may have no direct keyword match.
                # Safely load it before assigning its dependency bonus.
                if dependency not in candidates:
                    metadata = scan["modules"].get(dependency)
                    if metadata is None:
                        continue

                    relative = metadata["path"]
                    path = self._safe_path(relative)
                    if path is None:
                        continue

                    try:
                        helper_source = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        continue

                    candidates[dependency] = self._score_candidate(
                        dependency, relative, helper_source, keywords
                    )

                dependency_scores[dependency] = (
                    dependency_scores.get(dependency, 0) + 4
                )

        for module, bonus in dependency_scores.items():
            candidates[module]["score"] += bonus
            candidates[module]["dependency_bonus"] = bonus

        scored = list(candidates.values())

        # Prefer relevant implementation, then matching tests.
        scored.sort(
            key=lambda item: (
                -item["score"],
                item["is_test"],
                item["path"],
            )
        )

        selected = []
        total_bytes = 0

        for item in scored:
            if len(selected) >= self.MAX_CONTEXT_FILES:
                warnings.append("Context file limit reached")
                break

            encoded_size = len(item["source"].encode("utf-8"))

            if encoded_size > self.MAX_FILE_BYTES:
                warnings.append(f"File too large: {item['path']}")
                continue

            if total_bytes + encoded_size > self.MAX_TOTAL_BYTES:
                warnings.append("Context byte limit reached")
                continue

            total_bytes += encoded_size

            selected.append({
                "module": item["module"],
                "path": item["path"],
                "score": item["score"],
                "symbols": item["symbols"],
                "source": item["source"],
                "matched_terms": item["matched_terms"],
                "dependency_bonus": item.get("dependency_bonus", 0),
                "source_truncated": False,
            })

        selected_modules = {
            item["module"] for item in selected
        }

        # Report test modules actually selected, not guessed filenames.
        related_tests = sorted(
            item["module"]
            for item in selected
            if item["path"].startswith("tests/")
            or item["path"].startswith("workspace/test_")
            or item["path"].startswith("workspace/tests/")
        )

        impact = {}
        for module in sorted(selected_modules):
            impact[module] = self.scanner.analyze_impact(module, scan)

        return {
            "success": True,
            "keywords": keywords,
            "files": selected,
            "tests": related_tests,
            "impact": impact,
            "warnings": warnings,
            "summary": {
                "files_selected": len(selected),
                "bytes_selected": total_bytes,
                "modules_scanned": scan["summary"]["modules_scanned"],
            },
        }
