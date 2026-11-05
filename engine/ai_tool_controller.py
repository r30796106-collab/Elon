import json

from security.policy import PolicyError


class AIToolController:
    """Validates AI tool requests before passing them to ToolRegistry."""

    MAX_CALLS_PER_CYCLE = 5
    MAX_OUTPUT_CHARS = 16000

    def __init__(self, tool_registry):
        self.registry = tool_registry
        self.calls_used = 0

    def begin_cycle(self):
        """Explicitly reset the tool-call budget for a new task cycle."""
        self.calls_used = 0

    def _reject(self, reason):
        self.registry.engine.audit.append(
            "AI_TOOL_REQUEST_REJECTED",
            {"reason": str(reason)[:300]},
        )

    def handle_request(self, request):
        try:
            if not isinstance(request, dict):
                raise PolicyError("Tool request must be a JSON object")

            if set(request) != {"action", "tool", "arguments"}:
                raise PolicyError("Invalid or unexpected request fields")

            if request["action"] != "tool_call":
                raise PolicyError("Unsupported AI action")

            tool = request["tool"]
            arguments = request["arguments"]

            if not isinstance(tool, str) or tool not in self.registry.tools:
                raise PolicyError("Tool is not registered")

            if not isinstance(arguments, dict):
                raise PolicyError("Tool arguments must be an object")

            if set(arguments) != self.registry.tools[tool]:
                raise PolicyError("Invalid or unexpected tool arguments")

            if self.calls_used >= self.MAX_CALLS_PER_CYCLE:
                raise PolicyError("Tool-call limit reached for this cycle")

            self.calls_used += 1
            result = self.registry.execute(tool, arguments)

            serialized = json.dumps(
                result, ensure_ascii=False, separators=(",", ":")
            )

            if len(serialized) > self.MAX_OUTPUT_CHARS:
                result = {
                    "truncated": True,
                    "preview": serialized[:self.MAX_OUTPUT_CHARS],
                }

            return {
                "success": True,
                "tool": tool,
                "result": result,
            }

        except Exception as error:
            self._reject(type(error).__name__ + ": " + str(error))
            raise
