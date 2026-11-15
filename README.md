# ELON

ELON is a general-purpose developer-agent project. The V1 foundation prioritizes deterministic project inspection, task tracking, registered testing, controlled change workflows, crash recovery, and auditable operations.

AI-provider integrations remain in the repository for a later, separate milestone. They are not required for the deterministic V1 diagnostic and safety workflows.

## V1 capabilities

- Scan Python project modules and report syntax errors without executing scanned source files.
- Scan JavaScript modules, map local imports, and check syntax with Node.js parsing only; scanned source is never executed.
- Build project context and inspect approved workspace files through a registered tool interface.
- Run only explicitly registered test suites.
- Track development tasks and their lifecycle.
- Inspect Git branch, working-tree changes, diffs, and recent commits without modifying the repository.
- Generate bounded, deterministic read-only plans without an external AI provider.
- Prepare and review controlled patch proposals; applying changes is a separate approval-gated action.
- Keep backups and transaction journals for patch operations.
- Recover interrupted patch transactions and deliver recovery audit events through a durable outbox.
- Detect unfinished transactions, audit-chain problems, and cross-store inconsistencies with read-only health checks.
- Keep plan execution bounded, revalidate plans before execution, block replay, and require separate approval for modifications.

## Quick start

Run commands from the repository root:

```bash
python main.py create "Inspect project health" --description "Run diagnostics and review failures."
python main.py list
python main.py plan 1
python main.py diagnose
python main.py health
python main.py audit
```

Run the registered core test suite:

```bash
python main.py test
```

Run diagnostics with the full registered test suite:

```bash
python main.py diagnose --tests --test full
```

Inspect Git state without changing it:

```bash
python main.py git status
python main.py git diff
python main.py git log --limit 10
```

Inspect the registered tools and workspace:

```bash
python main.py tools registry
python main.py tools list
```

The `plan` command builds a deterministic, read-only inspection/analysis/test plan and submits it for explicit review. It does not call Gemini or another AI provider. Plan execution still requires approval and never modifies source files.

The `diagnose` command also checks JavaScript syntax when Node.js is available. It uses `node --check` only and never runs the scanned JavaScript source. If Node.js is unavailable while JavaScript files are present, the report warns and skips JavaScript syntax checks.

The `health` command is read-only. It checks required directories, SQLite integrity, the audit hash chain, and patch/proposal consistency. It does not perform recovery or call AI providers.

## Deterministic plan workflow

1. Create a task and note its task ID.
2. Generate a deterministic, read-only plan: `python main.py plan TASK_ID`.
3. Review the saved plan with `python main.py plans`.
4. Approve the exact plan ID: `python main.py plan-approve PLAN_ID`.
5. Execute the approved read-only plan: `python main.py execute-plan PLAN_ID`.

Plan execution is bounded to inspection, analysis, and registered tests. It cannot modify project files. A separate controlled patch proposal and approval workflow is required for source changes.

## Controlled patch workflow

Patch proposal generation, review, approval, and application are separate operations:

1. Inspect a proposal with `python main.py patches` and `python main.py patch-show PROPOSAL_ID`.
2. Approve or reject it explicitly with `python main.py patch-approve PROPOSAL_ID` or `python main.py patch-reject PROPOSAL_ID`.
3. Apply an approved proposal with `python main.py patch-apply PROPOSAL_ID`.
4. Review diagnostics and audit records afterward.

Patch application is policy-scoped, backed up, tested using a registered suite, and transactionally recoverable. Approval does not itself apply a patch.

## Recovery

Recovery is deliberately conservative. Do not confirm recovery while the original operation may still be running.

- Patch transaction recovery: `python main.py recover-patch TRANSACTION_ID --confirm-stopped`
- Patch/proposal state reconciliation: `python main.py reconcile-patch TRANSACTION_ID`
- Interrupted plan marking: `python main.py recover-plan PLAN_ID --confirm-stopped`

Plan recovery marks the execution interrupted and retains its lock. Automatic resume and replay are disabled; a human must inspect the situation before taking further action.

## Safety boundaries

1. No unrestricted shell execution.
2. Only registered test suites are eligible for automated execution.
3. File modifications must pass policy validation and the controlled patch workflow.
4. Automatic repair attempts are bounded (default maximum: five).
5. Credential access is prohibited.
6. Package installation requires explicit approval.
7. Deletion and deployment require approval.
8. Network operations require approval.
9. Real-money financial operations are prohibited.
10. Execution and recovery events must leave auditable evidence.

## Verification

GitHub Actions runs Python compilation and the complete unittest suite on pushes and pull requests using Python 3.12.

Run the same checks locally with:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Architecture

- `core/`: shared primitives and audit support
- `engine/`: task lifecycle, diagnostics, planning, transactions, and orchestration
- `providers/`: preserved provider integrations for a later milestone
- `security/`: permission and execution policies
- `tools/`: approved tool support
- `tests/`: automated verification
- `workspace/`: managed project files
- `logs/`: execution audit records
- `backups/`: recovery data
