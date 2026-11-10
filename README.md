# ELON

General-purpose autonomous AI development agent.

## Architecture

- core: Task understanding and planning
- engine: Task lifecycle and orchestration
- providers: AI provider integrations
- security: Permissions and execution policies
- tools: Approved tools
- tests: Automated verification
- workspace: Managed project files
- logs: Execution audit records
- backups: Recovery data

## Execution Policy

1. No unrestricted shell execution.
2. Only registered tests can execute automatically.
3. Scoped file modifications require policy validation.
4. Maximum five automatic repair attempts by default.
5. Credential access is prohibited.
6. Package installation requires approval.
7. Deletion and deployment require approval.
8. Network operations require approval.
9. Real-money financial operations are prohibited.
10. Every execution must produce auditable evidence.

## Development status

Initial foundation.
