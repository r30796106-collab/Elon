import json


class AIToolOrchestrator:
    """
    Runs a bounded Gemini -> controller -> registry tool cycle.
    Duplicate identical tool requests are blocked.
    """

    MAX_CALLS_PER_CYCLE = 5

    def __init__(self, provider, tool_controller):
        self.provider = provider
        self.controller = tool_controller

    def _audit_duplicate(self, tool, call_number):
        audit = self.controller.registry.engine.audit
        audit.append(
            "AI_TOOL_DUPLICATE_BLOCKED",
            {
                "tool": tool,
                "call_number": call_number,
                "reason": "Identical tool and arguments already executed",
            },
        )

    def run_cycle(self, task):
        if not isinstance(task, (str, dict)):
            raise ValueError("Task must be text or a task object")

        tools = self.controller.registry.tools
        if not isinstance(tools, dict) or not tools:
            raise ValueError("No registered tools are available")

        self.controller.begin_cycle()

        history = []
        calls = 0
        executed_requests = set()

        while calls < self.MAX_CALLS_PER_CYCLE:
            request_context = {
                "task": task,
                "tool_history": history,
            }

            proposal = self.provider.propose_tool_call(
                request_context,
                tools,
            )

            if not isinstance(proposal, dict):
                raise ValueError("Provider returned an invalid proposal")

            action = proposal.get("action")

            if action == "no_tool":
                if set(proposal) != {"action", "reason"}:
                    raise ValueError("Invalid no_tool proposal")
                if not isinstance(proposal["reason"], str):
                    raise ValueError("Invalid no_tool reason")

                return {
                    "success": True,
                    "status": "no_tool",
                    "reason": proposal["reason"][:500],
                    "tool_calls": calls,
                    "history": history,
                }

            if action != "tool_call":
                raise ValueError("Unsupported provider action")

            # Let the controller reject malformed requests independently.
            if (
                set(proposal) == {"action", "tool", "arguments"}
                and isinstance(proposal.get("tool"), str)
                and isinstance(proposal.get("arguments"), dict)
            ):
                request_key = json.dumps(
                    [proposal["tool"], proposal["arguments"]],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )

                if request_key in executed_requests:
                    self._audit_duplicate(proposal["tool"], calls + 1)
                    return {
                        "success": True,
                        "status": "duplicate_call_blocked",
                        "tool_calls": calls,
                        "blocked_tool": proposal["tool"],
                        "history": history,
                    }

            result = self.controller.handle_request(proposal)
            calls += 1

            # Record only after successful controller execution.
            executed_requests.add(
                json.dumps(
                    [result["tool"], proposal["arguments"]],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

            history.append({
                "tool": result["tool"],
                "success": result["success"],
                "result": result["result"],
            })

        return {
            "success": True,
            "status": "max_calls_reached",
            "tool_calls": calls,
            "history": history,
        }
