import json
from pathlib import Path

from security.policy import PolicyError


class ToolRegistry:
    MAX_READ_BYTES = 256 * 1024
    MAX_LIST_ITEMS = 200
    MAX_TEST_OUTPUT = 12000

    def __init__(self, engine):
        self.engine = engine
        self.tools = {
            "list_workspace_files": set(),
            "read_workspace_file": {"path"},
            "run_registered_tests": {"test_name"},
            "scan_project": set(),
            "analyze_project_impact": {"module"},
            "build_project_context": {"task_id"},
        }

    def _audit(self, tool, success, detail=""):
        self.engine.audit.append(
            "TOOL_EXECUTED",
            {
                "tool": tool,
                "success": bool(success),
                "detail": str(detail)[:300],
            },
        )

    def _list_workspace_files(self):
        root = self.engine.workspace
        results = []

        for current, dirs, files in __import__("os").walk(
            root, followlinks=False
        ):
            current_path = Path(current)

            dirs[:] = sorted(
                d for d in dirs
                if not (current_path / d).is_symlink()
                and d not in {"__pycache__", ".git"}
            )

            for filename in sorted(files):
                target = current_path / filename

                if target.is_symlink() or not target.is_file():
                    continue

                relative = target.relative_to(root).as_posix()

                try:
                    self.engine.policy.validate_workspace_file(relative)
                except (PolicyError, OSError):
                    continue

                results.append(relative)

                if len(results) >= self.MAX_LIST_ITEMS:
                    return results

        return results

    def _read_workspace_file(self, path):
        if not isinstance(path, str) or not path.strip():
            raise PolicyError("A valid workspace path is required")

        target = self.engine.policy.validate_workspace_file(path)

        if target.stat().st_size > self.MAX_READ_BYTES:
            raise PolicyError("File exceeds the 256 KB read limit")

        return target.read_text(encoding="utf-8")

    MAX_CONTEXT_OUTPUT_CHARS = 14000

    def _build_project_context(self, task_id):
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise PolicyError("Task ID must be an integer")

        if task_id <= 0:
            raise PolicyError("Task ID must be positive")

        task = self.engine.get_task(task_id)
        if task is None:
            raise PolicyError("Task does not exist")

        from engine.project_context import ProjectContextBuilder

        builder = ProjectContextBuilder(self.engine.root)
        result = builder.build({
            "title": task["title"],
            "description": task["description"],
        })

        result["task_id"] = task_id

        # Bound the serialized response before it reaches the AI controller.
        # Preserve valid JSON and clearly mark any shortened source.
        def serialized_size():
            return len(json.dumps(
                result, ensure_ascii=False, separators=(",", ":")
            ))

        while serialized_size() > self.MAX_CONTEXT_OUTPUT_CHARS:
            candidates = [
                item for item in result.get("files", [])
                if item.get("source")
            ]

            if not candidates:
                raise PolicyError(
                    "Context metadata exceeds the output size limit"
                )

            largest = max(candidates, key=lambda item: len(item["source"]))
            current = largest["source"]
            largest["source"] = current[:len(current) // 2]
            largest["source_truncated"] = True

            warning = "One or more source files were shortened for output limits"
            if warning not in result["warnings"]:
                result["warnings"].append(warning)

        result["output_chars"] = serialized_size()
        return result

    def execute(self, name, arguments=None):
        tool_name = name if isinstance(name, str) else "invalid_name"

        try:
            if not isinstance(name, str) or name not in self.tools:
                raise PolicyError("Tool is not registered")

            if arguments is None:
                arguments = {}

            if not isinstance(arguments, dict):
                raise PolicyError("Tool arguments must be an object")

            expected = self.tools[name]
            if set(arguments) != expected:
                raise PolicyError("Invalid or unexpected tool arguments")

            if name == "list_workspace_files":
                result = self._list_workspace_files()

            elif name == "read_workspace_file":
                result = self._read_workspace_file(arguments["path"])

            elif name == "run_registered_tests":
                test_name = arguments["test_name"]

                if not isinstance(test_name, str):
                    raise PolicyError("Test name must be text")

                # Validate independently before calling the engine.
                self.engine.policy.validate_test(test_name)

                result = self.engine.run_registered_tests(test_name)
                result = dict(result)
                result["stdout"] = str(
                    result.get("stdout", "")
                )[:self.MAX_TEST_OUTPUT]
                result["stderr"] = str(
                    result.get("stderr", "")
                )[:self.MAX_TEST_OUTPUT]

            elif name == "scan_project":
                from engine.project_intelligence import ProjectScanner

                scanner = ProjectScanner(self.engine.root)
                scan = scanner.scan()
                result = {
                    "success": scan["success"],
                    "summary": scan["summary"],
                    "modules": sorted(scan["modules"]),
                    "dependencies": scan["dependencies"],
                    "errors": scan["errors"],
                    "skipped": scan["skipped"],
                }

            elif name == "analyze_project_impact":
                from engine.project_intelligence import ProjectScanner

                module = arguments["module"]
                if not isinstance(module, str) or not module.strip():
                    raise PolicyError("A valid module name is required")

                scanner = ProjectScanner(self.engine.root)
                result = scanner.analyze_impact(module.strip())

            elif name == "build_project_context":
                result = self._build_project_context(arguments["task_id"])

            else:
                raise PolicyError("Tool is not registered")

            self._audit(name, True, "completed")
            return result

        except Exception as error:
            self._audit(tool_name, False, type(error).__name__)
            raise
