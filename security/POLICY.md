# ELON Security Policy

Default mode: AUTONOMOUS WITH RESTRICTED HIGH-IMPACT ACTIONS

Automatic permissions:
- Read and modify permitted files inside Termux home.
- Modify registered project files.
- Run registered tests.
- Perform up to 5 automatic repair attempts per task.
- Create backups before modifications.
- Package installation is blocked by default.
  Explicit user approval is required before any package installation.
- Create logs and audit records.

Approval required:
- Downloads larger than 250 MiB.
- Permanent file or folder deletion.
- Bulk deletion.
- Arbitrary shell commands.
- Credential access.
- Deployment and publishing.
- Unapproved network operations.

Protected:
- Credential and private-key files.
- Android system areas.
- ELON security policy, security module and audit implementation.
- ELON backup and log directories.

Prohibited:
- Real-money trading.
- Unauthorized credential access.
- Unrestricted system execution.
- Disabling security controls.

Maximum automatic repair attempts: 5.

Note:
This is application-level policy enforcement, not an operating-system
sandbox. Download, installation and deletion execution workflows must
enforce these checks before performing those operations.
