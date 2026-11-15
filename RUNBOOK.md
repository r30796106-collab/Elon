# ELON Operational Runbook

This runbook describes the deterministic, non-AI workflow available in the current development branch. It does not authorize plan approval, execution, recovery, package installation, or Git writes by itself.

## Safety boundaries

- Gemini, Aider, OpenRouter, and other model integrations are preserved but paused.
- The deterministic `plan` command does not call an AI provider.
- Read-only plans can inspect files, analyze dependencies, and run registered tests. They cannot modify source files.
- Source changes use the separate patch proposal → explicit approval → apply → test → rollback workflow.
- Package installation remains blocked unless separately and explicitly approved.
- Git commands exposed by ELON are read-only: status, diff, and log.
- Real-money trading, automatic deployment, unrestricted shell execution, and credential access are prohibited.
- Never attempt recovery while the original execution/patch process may still be running.

## Pre-flight checks

Run from the ELON repository root:

```bash
python main.py environment
python main.py health
python main.py diagnose
python main.py git status
```

For full verification:

```bash
python main.py diagnose --tests --test full
```

Performance baseline (read-only):

```bash
python main.py benchmark --iterations 3
```

## Project scaffolding

ELON can generate a minimal Python or JavaScript project template from a project name, description, and requirements text. It creates a pending proposal first; no project files are written until explicit approval.

```bash
python main.py scaffold-propose demo_app --language python --description "A small demo application" --requirements "Provide a main entry point and smoke test."
python main.py scaffold-list
python main.py scaffold-approve PROPOSAL_ID
python main.py scaffold-apply PROPOSAL_ID
```

Generated projects are restricted to `workspace/generated_projects/`. Existing destinations are never overwritten. No packages are installed and generated code is not executed. If application is interrupted, confirm the original process has stopped before recovery:

```bash
python main.py scaffold-recover PROPOSAL_ID --confirm-stopped
```

## Deterministic task workflow

1. Create a task:

   ```bash
   python main.py create "Review project health" --description "Inspect errors and verify registered tests."
   ```

2. List tasks and note the task ID:

   ```bash
   python main.py list
   ```

3. Generate a deterministic, read-only plan:

   ```bash
   python main.py plan TASK_ID
   ```

4. List plans and inspect the plan ID:

   ```bash
   python main.py plans
   ```

5. Inspect plan progress/checkpoints:

   ```bash
   python main.py plan-progress PLAN_ID
   ```

6. Only after an explicit human decision, approve and execute:

   ```bash
   python main.py plan-approve PLAN_ID
   python main.py execute-plan PLAN_ID
   ```

Execution is not performed by `plan` or `plan-approve`. The execution command is a separate action.

## Patch workflow

Patch proposals are separate from read-only plans. A multi-file proposal can be prepared without an AI provider by placing a JSON file under `workspace/`:

```json
{
  "task_id": 1,
  "summary": "Update two related modules",
  "test_name": "full",
  "patches": [
    {
      "path": "workspace/app.py",
      "expected_sha256": "64-character SHA-256 of the current file",
      "content": "VALUE = 2\n"
    },
    {
      "path": "workspace/helpers.py",
      "expected_sha256": "64-character SHA-256 of the current file",
      "content": "OFFSET = 3\n"
    }
  ]
}
```

Calculate the expected hashes before preparing the JSON:

```bash
sha256sum workspace/app.py workspace/helpers.py
```

Submit it as a pending proposal; this does not change source files:

```bash
python main.py patch-propose-json workspace/proposal.json
```

Review the complete patch before approval:

```bash
python main.py patches
python main.py patch-show PROPOSAL_ID
python main.py patch-approve PROPOSAL_ID
python main.py patch-apply PROPOSAL_ID
```

Do not approve a patch unless its target files, expected hashes, backup scope, and registered test suite have been reviewed. Failed tests should trigger rollback; inspect the resulting proposal and transaction status before retrying.

## Crash recovery

### Interrupted plan

1. Confirm the original ELON process is fully stopped.
2. Inspect the saved plan and progress:
   ```bash
   python main.py plans
   python main.py plan-progress PLAN_ID
   ```
3. Only after confirming the original process has stopped, run:
   ```bash
   python main.py recover-plan PLAN_ID --confirm-stopped
   ```
4. The execution lock is retained to prevent accidental replay. Recovery does not resume the plan automatically.

### Interrupted patch transaction

1. Confirm the original patch process is fully stopped.
2. Inspect the transaction and proposal state:
   ```bash
   python main.py patches
   python main.py status
   ```
3. Only after confirming the original process has stopped, run:
   ```bash
   python main.py recover-patch TRANSACTION_ID --confirm-stopped
   ```
4. Verify health and audit integrity:
   ```bash
   python main.py health
   python main.py audit
   ```

Never remove execution locks, backups, audit records, or recovery state manually.

## Git inspection

```bash
python main.py git status
python main.py git diff
python main.py git log --limit 10
```

These commands do not stage, commit, switch branches, reset, push, or fetch.

## Known limitations

- AI-driven proposal generation and automatic debugging are paused by project direction.
- JavaScript support currently covers bounded syntax diagnostics and local import mapping; JavaScript source modification is not yet enabled.
- New-file project generation is not enabled by the current modification policy.
- Git write operations and dependency installation are intentionally unavailable.
- Long-running plan progress is checkpointed and inspectable, but automatic background scheduling and automatic resume are not enabled.
- Android/Termux validation remains a required final acceptance step; CI results are not a substitute for device validation.
