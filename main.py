import argparse
import json
import os
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.health_check import HealthChecker
from core.ai_bridge import AIBridge
from core.self_repair import SelfRepairEngine
from engine.autonomous_controller import AutonomousController
from engine.tool_registry import ToolRegistry
from engine.local_bridge import LocalBridgeServer
from engine.ai_tool_controller import AIToolController
from engine.ai_tool_orchestrator import AIToolOrchestrator
from engine.ai_task_planner import AITaskPlanner
from engine.plan_approval import PlanApprovalManager
from engine.plan_executor import PlanExecutor
from engine.patch_approval import PatchApprovalManager
from engine.patch_transaction import PatchTransaction
from engine.patch_proposal_service import PatchProposalService
from core.usage_tracker import UsageTracker
from providers.gemini_provider import GeminiProvider as OpenAIProvider

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(
        description="ELON Autonomous AI Developer"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("title")
    create.add_argument("--description", default="")

    prepare = sub.add_parser("prepare")
    prepare.add_argument("task_id", type=int)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("task_id", type=int)
    ingest.add_argument("response_file")

    repair = sub.add_parser("repair")
    repair.add_argument("task_id", type=int)
    repair.add_argument("--test", default="core")
    repair.add_argument("--multi-file", action="store_true")

    develop = sub.add_parser("develop")
    develop.add_argument("task_id", type=int)
    develop.add_argument("--test", default="core")

    sub.add_parser("patches")
    patch_show = sub.add_parser("patch-show")
    patch_show.add_argument("proposal_id")
    patch_approve = sub.add_parser("patch-approve")
    patch_approve.add_argument("proposal_id")
    patch_reject = sub.add_parser("patch-reject")
    patch_reject.add_argument("proposal_id")
    patch_apply = sub.add_parser("patch-apply")
    patch_apply.add_argument("proposal_id")

    recover_patch = sub.add_parser("recover-patch")
    recover_patch.add_argument("transaction_id")
    recover_patch.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Confirm the original patch process has completely stopped.",
    )

    reconcile_patch = sub.add_parser("reconcile-patch")
    reconcile_patch.add_argument("transaction_id")

    sub.add_parser("list")
    sub.add_parser("test")
    sub.add_parser("audit")
    sub.add_parser("status")
    sub.add_parser("usage")
    sub.add_parser("health")

    bridge_command = sub.add_parser("bridge")
    bridge_command.add_argument("--port", type=int, default=8765)

    plan_command = sub.add_parser("plan")
    plan_command.add_argument("task_id", type=int)

    sub.add_parser("plans")

    approve_command = sub.add_parser("plan-approve")
    approve_command.add_argument("plan_id")

    approve_interactive = sub.add_parser(
        "approve", help="Interactively approve a saved plan"
    )
    approve_interactive.add_argument("plan_id")

    reject_command = sub.add_parser("plan-reject")
    reject_command.add_argument("plan_id")
    execute_plan_command = sub.add_parser("execute-plan")
    execute_plan_command.add_argument("plan_id")
    recover_plan_command = sub.add_parser("recover-plan")
    recover_plan_command.add_argument("plan_id")
    recover_plan_command.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Confirm the original execution process has completely stopped.",
    )

    tool_cycle = sub.add_parser("tool-cycle")
    tool_cycle.add_argument("task_id", type=int)

    tools = sub.add_parser("tools")
    tool_sub = tools.add_subparsers(dest="tool_action", required=True)

    tool_sub.add_parser("list")

    read_tool = tool_sub.add_parser("read")
    read_tool.add_argument("path")

    test_tool = tool_sub.add_parser("test")
    test_tool.add_argument("test_name")

    tool_sub.add_parser("registry")

    context_tool = tool_sub.add_parser("context")
    context_tool.add_argument("task_id", type=int)


    args = parser.parse_args()

    # Health is intentionally handled before TaskEngine initialization.
    if args.command == "health":
        report = HealthChecker(ROOT).run()
        print("ELON RUNTIME HEALTH:", report["status"])
        for name, result in report["checks"].items():
            print(f"{name.upper()}: {result['status']} - {result['message']}")
        print("Read-only:", report["read_only"])
        print("Automatic recovery:", "DISABLED")
        print("AI calls:", "DISABLED")
        if report["status"] == "FAIL":
            raise SystemExit(2)
        return

    engine = TaskEngine(ROOT)

    try:
        if args.command == "bridge":
            token = os.environ.get("ELON_BRIDGE_TOKEN", "")
            if len(token) < 32:
                raise RuntimeError("ELON_BRIDGE_TOKEN must contain at least 32 characters.")
            server = LocalBridgeServer(engine, token, port=args.port)
            print(f"ELON LOCAL BRIDGE: http://127.0.0.1:{server.port}")
            print("Authentication: Bearer token from environment")
            print("Access: localhost only; read-only tools")
            print("Stop with CTRL+C")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nELON Local Bridge stopped.")
            finally:
                server.close()
        elif args.command == "create":
            task_id = engine.create_task(
                args.title, args.description
            )
            print(f"Task created: #{task_id}")

        elif args.command == "prepare":
            bridge = AIBridge(ROOT)
            output = bridge.prepare_task(engine, args.task_id)
            print("AI REQUEST PREPARED")
            print("File:", output)

        elif args.command == "ingest":
            bridge = AIBridge(ROOT)
            output = bridge.ingest_response(
                engine, args.task_id, args.response_file
            )
            print("AI PLAN INGESTED")
            print("File:", output)
            print("Execution: NOT performed")

        elif args.command == "repair":
            service = PatchProposalService(engine)
            result = service.generate_repair(
                args.task_id, args.test, args.multi_file
            )
            print("REPAIR:", result["message"])
            proposal = result.get("proposal")
            if proposal:
                print("Proposal ID:", proposal["proposal_id"])
                print("Status:", proposal["status"])
                print("Files:", len(proposal["patches"]))
            print("Source changes: NOT performed")

        elif args.command == "develop":
            service = PatchProposalService(engine)
            result = service.generate_development(
                args.task_id, args.test
            )
            print("DEVELOPMENT:", result["message"])
            proposal = result.get("proposal")
            if proposal:
                print("Proposal ID:", proposal["proposal_id"])
                print("Status:", proposal["status"])
                print("Files:", len(proposal["patches"]))
            print("Source changes: NOT performed")
            if not result["success"]:
                raise SystemExit(2)

        elif args.command == "patches":
            manager = PatchApprovalManager(engine)
            records = manager.list_proposals()
            for record in records:
                print(
                    record["proposal_id"],
                    "| task =", record["task_id"],
                    "|", record["status"],
                    "| files =", len(record["patches"]),
                    "|", record["summary"],
                )

        elif args.command == "patch-show":
            manager = PatchApprovalManager(engine)
            record = manager.get_proposal(args.proposal_id)

            print("PROPOSAL:", record["proposal_id"])
            print("TASK:", record["task_id"])
            print("STATUS:", record["status"])
            print("SUMMARY:", record["summary"])
            print("TEST SUITE:", record["test_name"])

            for patch in record["patches"]:
                print("FILE:", patch["path"])
                print("  OLD SHA256:", patch["expected_sha256"])
                print("  NEW SHA256:", patch["new_sha256"])
                print("  CONTENT BYTES:", len(patch["content"].encode("utf-8")))

            print("\\n=== READ-ONLY UNIFIED DIFF ===")
            for preview in manager.preview_diff(args.proposal_id):
                print("\\nFILE:", preview["path"])
                if preview["diff"]:
                    print("".join(preview["diff"]), end="")
                else:
                    print("(No differences)")

            print("\\nSource changes: NOT performed")

        elif args.command == "patch-approve":
            record = PatchApprovalManager(engine).approve(
                args.proposal_id
            )
            print("PATCH PROPOSAL APPROVED")
            print("Proposal ID:", record["proposal_id"])
            print("Status:", record["status"])
            print("Execution: NOT performed")

        elif args.command == "patch-reject":
            record = PatchApprovalManager(engine).reject(
                args.proposal_id
            )
            print("PATCH PROPOSAL REJECTED")
            print("Proposal ID:", record["proposal_id"])
            print("Status:", record["status"])
            print("Execution: NOT performed")

        elif args.command == "patch-apply":
            manager = PatchApprovalManager(engine)
            result = manager.apply_approved(
                args.proposal_id, PatchTransaction(engine)
            )
            print("PATCH APPLY RESULT")
            print("Status:", result["status"])
            print("Tests:", "PASS" if result["tests"]["success"] else "FAIL")
            if result["success"]:
                print("Files changed:", len(result["patches"]))
            else:
                print("Rollback:", result.get("rollback", "check warnings"))
                raise SystemExit(2)

        elif args.command == "recover-patch":
            result = PatchTransaction(engine).recover(
                args.transaction_id,
                confirm_stopped=args.confirm_stopped,
            )
            print("PATCH TRANSACTION RECOVERY")
            print("Transaction ID:", result["transaction_id"])
            print("Status:", result["status"])
            print("Files checked:", result["files_checked"])
            print("Files restored:", result["files_restored"])
            print("Repair attempts incremented: NO")

        elif args.command == "reconcile-patch":
            result = PatchApprovalManager(engine).reconcile_transaction(
                args.transaction_id
            )
            print("PATCH TRANSACTION RECONCILIATION")
            print("Action:", result["action"])
            print("Transaction ID:", result["transaction_id"])
            print("Transaction status:", result["transaction_status"])
            if result.get("proposal_id"):
                print("Proposal ID:", result["proposal_id"])
            if result.get("proposal_status"):
                print("Proposal status:", result["proposal_status"])
            print("Automatic recovery: NOT performed")
            print("Patch application: NOT performed")

        elif args.command == "list":
            for task in engine.list_tasks():
                print(
                    f"#{task['id']} | {task['status']} | "
                    f"{task['title']} | repairs={task['repair_attempts']}"
                )

        elif args.command == "test":
            result = engine.run_registered_tests()
            print(result.get("stdout", ""))
            print(result.get("stderr", ""))
            print("TEST:", "PASS" if result["success"] else "FAIL")

        elif args.command == "audit":
            print(
                "AUDIT:",
                "VALID" if engine.audit_valid() else "INVALID"
            )

        elif args.command == "tool-cycle":
            task = engine.get_task(args.task_id)
            if task is None:
                raise ValueError("Task does not exist")

            provider = OpenAIProvider()
            if not provider.configured():
                raise RuntimeError("Gemini API key is not configured")

            registry = ToolRegistry(engine)
            tool_controller = AIToolController(registry)
            orchestrator = AIToolOrchestrator(
                provider, tool_controller
            )
            task_context = {
                "id": task["id"],
                "title": task["title"],
                "description": task["description"],
            }
            result = orchestrator.run_cycle(task_context)
            print("AI TOOL CYCLE:", result["status"])
            print("Tool calls:", result["tool_calls"])
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "tools":
            registry = ToolRegistry(engine)

            if args.tool_action == "list":
                files = registry.execute("list_workspace_files")
                print("WORKSPACE FILES:")
                for item in files:
                    print("-", item)

            elif args.tool_action == "read":
                content = registry.execute(
                    "read_workspace_file",
                    {"path": args.path},
                )
                print(content)

            elif args.tool_action == "test":
                result = registry.execute(
                    "run_registered_tests",
                    {"test_name": args.test_name},
                )
                print(result.get("stdout", ""))
                print(result.get("stderr", ""))
                print("TEST:", "PASS" if result["success"] else "FAIL")
                if not result["success"]:
                    raise SystemExit(2)

            elif args.tool_action == "registry":
                print("REGISTERED ELON TOOLS:")
                for name, required_args in sorted(registry.tools.items()):
                    print(f"- {name} | required arguments: {sorted(required_args)}")

            elif args.tool_action == "context":
                result = registry.execute(
                    "build_project_context",
                    {"task_id": args.task_id},
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "plan":
            task = engine.get_task(args.task_id)
            if task is None:
                raise ValueError("Task does not exist")

            provider = OpenAIProvider()
            if not provider.configured():
                raise RuntimeError("Gemini API key is not configured")

            planner = AITaskPlanner(ROOT, provider)
            result = planner.plan({
                "title": task["title"],
                "description": task["description"],
            })

            manager = PlanApprovalManager(ROOT)
            saved = manager.save_plan(args.task_id, result)

            print("PLAN SUBMITTED FOR REVIEW")
            print("Plan ID:", saved["plan_id"])
            print("Status:", saved["status"])
            print("Plan hash:", saved["plan_hash"])
            print("Execution: NOT performed")
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "plans":
            manager = PlanApprovalManager(ROOT)
            records = manager.list_plans()
            print(json.dumps(records, ensure_ascii=False, indent=2))

        elif args.command == "approve":
            manager = PlanApprovalManager(ROOT)
            print("PLAN APPROVAL REQUEST")
            print("Plan ID:", args.plan_id)
            print("Approval does NOT execute the plan.")
            try:
                answer = input(
                    "Press Enter to approve, or type anything to cancel: "
                )
            except (EOFError, KeyboardInterrupt):
                print("\\nAPPROVAL CANCELLED")
                return

            if answer != "":
                print("APPROVAL CANCELLED")
                return

            record = manager.approve(args.plan_id)
            print("PLAN APPROVED")
            print("Plan ID:", record["plan_id"])
            print("Status:", record["status"])
            print("Plan hash:", record["plan_hash"])
            print("Execution: NOT performed")

        elif args.command == "plan-approve":
            manager = PlanApprovalManager(ROOT)
            record = manager.approve(args.plan_id)
            print("PLAN APPROVED")
            print("Plan ID:", record["plan_id"])
            print("Status:", record["status"])
            print("Plan hash:", record["plan_hash"])
            print("Execution: NOT performed")

        elif args.command == "plan-reject":
            manager = PlanApprovalManager(ROOT)
            record = manager.reject(args.plan_id)
            print("PLAN REJECTED")
            print("Plan ID:", record["plan_id"])
            print("Status:", record["status"])
            print("Plan hash:", record["plan_hash"])
            print("Execution: NOT performed")
        elif args.command == "execute-plan":
            manager = PlanApprovalManager(ROOT)
            executor = PlanExecutor(engine, manager)
            result = executor.execute(args.plan_id)
            print("PLAN EXECUTION RESULT")
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "recover-plan":
            manager = PlanApprovalManager(ROOT)
            record = manager.mark_interrupted(
                args.plan_id,
                confirm_stopped=args.confirm_stopped,
            )
            print("PLAN EXECUTION MARKED INTERRUPTED")
            print("Plan ID:", record["plan_id"])
            print("Execution:", record["execution"]["state"])
            print("Replay: BLOCKED (lock retained)")
            print("Automatic resume: DISABLED")

        elif args.command == "usage":
            print(UsageTracker().report())

        elif args.command == "status":
            provider = OpenAIProvider()

            print("ELON AI Developer")
            print("Project:", ROOT)
            print("Execution policy: Autonomous with restricted high-impact actions")
            print(
                "AI provider:",
                "Gemini API configured"
                if provider.configured()
                else "Gemini API key not configured"
            )
            print("Model:", provider.model)
            print("Automatic repairs:", engine.policy.MAX_REPAIRS)
            print("Download approval threshold: 250 MiB")
            print("Package installation: BLOCKED by default; explicit approval required")
            print("Real-money trading: PROHIBITED")
            print("Tasks:", len(engine.list_tasks()))
            print(
                "Audit:",
                "VALID" if engine.audit_valid() else "INVALID"
            )

    except SystemExit:
        raise
    except (ValueError, OSError, RuntimeError) as error:
        print("ERROR:", error)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
