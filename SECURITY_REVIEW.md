# ELON Security Controls and Residual Risks

This is an internal engineering review, not an independent security certification or penetration-test report. It describes the deterministic development branch and must be revalidated before release.

## Current controls

### Execution boundaries
- Plan execution is limited to inspection, analysis, and registered test operations.
- Source modification is separate and requires a saved patch proposal, explicit approval, backup, registered tests, and rollback on failure.
- No unrestricted shell tool is exposed through the local bridge or registered tool interface.
- Git CLI integration is read-only; it does not stage, commit, switch, reset, push, or fetch.
- JavaScript diagnostics use Node `--check` only; source files are not executed.
- Environment inspection reads manifests and runtime versions; it does not install packages or use the network.
- Project scaffolding creates new files only under `workspace/generated_projects/` after explicit approval; it refuses existing destinations, uses exclusive file creation, records hashes in a manifest, and supports confirmed recovery without deleting or overwriting user files.

### Path and file safety
- Project operations use configured project roots and protected-path rules.
- Patch application verifies expected file hashes and writes backups before replacing source files.
- Patch recovery validates transaction state, backup integrity, and expected file hashes before restoration.
- Symlinked JavaScript files and dependency manifests are excluded or constrained to the project root.
- Plan records validate task identity, step IDs, execution state, and checkpoint metadata when loaded.

### Approval and recovery
- Plan approval and proposal approval are explicit state transitions.
- Approval evidence is appended and synced before the corresponding approval state is persisted.
- Execution uses an exclusive lock and durable claim record.
- Interrupted execution requires explicit confirmation that the original process has stopped; locks are retained to prevent accidental replay.
- Completed-step checkpoints are persisted and can be reconstructed from verified audit events.
- Patch recovery audit events remain in a SQLite outbox until the audit log is synced.

### Diagnostics and verification
- Health checks verify database integrity, audit hash-chain integrity, and patch/proposal consistency.
- Python syntax scanning is static and does not execute scanned modules.
- JavaScript syntax scanning is parser-only and bounded by file-count, file-size, per-file timeout, and total time limits.
- GitHub Actions compiles Python sources, runs the full unittest discovery suite, and runs read-only performance benchmarks.

## Residual risks and limitations

- The audit implementation itself remains unchanged by project policy; durability is strengthened at critical call sites, but this does not replace an independently reviewed audit subsystem.
- Local administrators or a process with write access to the repository/data directories can still tamper with local files and metadata.
- Plan and proposal files are locally stored; there is no external signature service or hardware-backed integrity key.
- Plan approval and interruption-recovery operations need further concurrency review before multi-process production use.
- JavaScript support is currently read-only diagnostics/import mapping; JavaScript code modification is not enabled.
- New-file project generation is not enabled by the current protected modification policy.
- Git write operations, dependency installation, automatic deployment, and background scheduling are not enabled.
- Gemini/Aider/OpenRouter integrations are preserved but paused; AI-dependent workflows have not been validated in this phase.
- Android/Termux behavior has not been revalidated by the latest GitHub Actions runs.
- No independent security audit or penetration test has been performed.

## Release blockers

Before calling ELON production-ready:
1. Complete the independent review of approval, lock, audit, and recovery concurrency behavior.
2. Run the documented pre-flight and recovery procedures on the target Android/Termux device.
3. Validate backup/restore behavior on the target filesystem and after forced process termination.
4. Review every protected-path and package-install policy with the user.
5. Decide explicitly whether to resume AI integrations; do not enable them implicitly.
6. Confirm all draft PRs are reviewed and merged by an authorized human.
