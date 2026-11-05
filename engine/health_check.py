import json
import sqlite3
from contextlib import closing
from pathlib import Path

from core.audit import AuditLog


class HealthChecker:
    """Read-only runtime, integrity and cross-store consistency checks."""

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()

    def _database_check(self):
        path = self.root / "elon.sqlite3"

        if not path.is_file():
            return {"status": "FAIL", "message": "SQLite database is missing."}

        try:
            uri = path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                result = db.execute("PRAGMA integrity_check").fetchone()

                if not result or result[0] != "ok":
                    return {"status": "FAIL", "message": str(result)}

                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

                required = {"patch_transactions"}
                missing = required - tables
                if missing:
                    return {
                        "status": "FAIL",
                        "message": "Missing tables: " + ", ".join(sorted(missing)),
                    }

                unfinished = db.execute(
                    """
                    SELECT transaction_id, status
                    FROM patch_transactions
                    WHERE status IN ('applying', 'recovery_required')
                    """
                ).fetchall()

                return {
                    "status": "WARN" if unfinished else "PASS",
                    "message": (
                        f"SQLite integrity OK; {len(unfinished)} unfinished "
                        "patch transaction(s)."
                    ),
                    "unfinished_transactions": [
                        {"transaction_id": row[0], "status": row[1]}
                        for row in unfinished
                    ],
                }

        except (sqlite3.Error, OSError, ValueError) as error:
            return {
                "status": "FAIL",
                "message": f"Database check failed: {error}",
            }

    def _read_audit_records(self):
        path = self.root / "logs" / "audit.jsonl"
        if not path.exists():
            return []

        records = []
        previous = "GENESIS"

        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            record = json.loads(line)

            if not isinstance(record, dict):
                raise ValueError(f"Audit record {number} is not an object.")

            supplied_hash = record.pop("hash")
            if record.get("previous_hash") != previous:
                raise ValueError(f"Audit chain mismatch at line {number}.")

            if AuditLog.digest(record) != supplied_hash:
                raise ValueError(f"Audit hash mismatch at line {number}.")

            previous = supplied_hash
            records.append(record)

        return records

    def _audit_check(self):
        path = self.root / "logs" / "audit.jsonl"

        if not path.exists():
            return {
                "status": "PASS",
                "message": "Audit log does not exist yet; no records to verify.",
            }

        try:
            records = self._read_audit_records()
            return {
                "status": "PASS",
                "message": f"Audit hash chain is valid ({len(records)} records).",
            }
        except (
            OSError, ValueError, KeyError, TypeError, json.JSONDecodeError
        ) as error:
            return {
                "status": "FAIL",
                "message": f"Audit verification failed: {error}",
            }

    def _consistency_check(self):
        """Compare transaction journal, proposal files and audit evidence.

        This method is diagnostic only. It never writes to any store.
        """
        db_path = self.root / "elon.sqlite3"
        proposal_dir = self.root / "workspace" / "patch_proposals"

        try:
            audit_records = self._read_audit_records()

            events_by_transaction = {}
            events_by_proposal = {}

            for record in audit_records:
                event_name = record.get("event")
                details = record.get("details") or {}

                transaction_id = details.get("transaction_id")
                proposal_id = details.get("proposal_id")

                if transaction_id:
                    events_by_transaction.setdefault(
                        transaction_id, []
                    ).append(event_name)

                if proposal_id:
                    events_by_proposal.setdefault(
                        proposal_id, []
                    ).append(event_name)

            proposals = {}
            if proposal_dir.is_dir():
                for path in sorted(proposal_dir.glob("*.json")):
                    with path.open("r", encoding="utf-8") as handle:
                        record = json.load(handle)

                    if not isinstance(record, dict):
                        raise ValueError(
                            f"Proposal file is not an object: {path.name}"
                        )

                    proposal_id = record.get("proposal_id")
                    if not isinstance(proposal_id, str) or path.stem != proposal_id:
                        raise ValueError(
                            f"Proposal ID/filename mismatch: {path.name}"
                        )

                    if proposal_id in proposals:
                        raise ValueError(f"Duplicate proposal ID: {proposal_id}")

                    proposals[proposal_id] = record

            warnings = []
            critical = []
            transaction_count = 0

            uri = db_path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

                if not {"patch_transactions", "patch_transaction_files"}.issubset(tables):
                    return {
                        "status": "PASS",
                        "message": (
                            "Cross-store checks skipped: minimal transaction schema; "
                            "database integrity and unfinished-transaction checks remain active."
                        ),
                        "warnings": [],
                        "critical_issues": [],
                        "read_only": True,
                    }

                columns = {
                    row[1]
                    for row in db.execute(
                        "PRAGMA table_info(patch_transactions)"
                    )
                }
                if not {
                    "transaction_id", "task_id", "proposal_id", "status"
                }.issubset(columns):
                    return {
                        "status": "PASS",
                        "message": (
                            "Cross-store checks skipped: transaction schema "
                            "does not contain all diagnostic fields."
                        ),
                        "warnings": [],
                        "critical_issues": [],
                        "read_only": True,
                    }

                rows = db.execute(
                    """
                    SELECT transaction_id, task_id, proposal_id, status
                    FROM patch_transactions
                    """
                ).fetchall()

                transaction_count = len(rows)

                for transaction_id, task_id, proposal_id, status in rows:
                    file_count = db.execute(
                        """
                        SELECT COUNT(*)
                        FROM patch_transaction_files
                        WHERE transaction_id = ?
                        """,
                        (transaction_id,),
                    ).fetchone()[0]

                    event_names = events_by_transaction.get(transaction_id, [])
                    applied_events = event_names.count("PATCH_APPLIED")
                    summary_events = event_names.count("PATCH_SET_APPLIED")

                    if status in ("applying", "recovery_required"):
                        warnings.append(
                            f"{transaction_id}: {status}; manual recovery review required."
                        )

                    if status == "applied":
                        if applied_events != file_count:
                            warnings.append(
                                f"{transaction_id}: expected {file_count} "
                                f"PATCH_APPLIED event(s), found {applied_events}."
                            )

                        if summary_events != 1:
                            warnings.append(
                                f"{transaction_id}: expected one PATCH_SET_APPLIED "
                                f"event, found {summary_events}."
                            )

                    if proposal_id:
                        proposal = proposals.get(proposal_id)

                        if proposal is None:
                            critical.append(
                                f"{transaction_id}: linked proposal file "
                                f"{proposal_id} is missing."
                            )
                        elif proposal.get("task_id") != task_id:
                            critical.append(
                                f"{transaction_id}: linked proposal task ID mismatch."
                            )
                        else:
                            proposal_status = proposal.get("status")

                            if status == "applied" and proposal_status == "applying":
                                warnings.append(
                                    f"{transaction_id}: transaction is applied but "
                                    "proposal remains applying; test outcome needs review."
                                )

                            if status == "rolled_back" and proposal_status == "applying":
                                warnings.append(
                                    f"{transaction_id}: rolled-back transaction has "
                                    "an applying proposal; reconciliation review required."
                                )

            for proposal_id, proposal in proposals.items():
                status = proposal.get("status")
                event_names = events_by_proposal.get(proposal_id, [])

                expected = {
                    "pending": ("PATCH_PROPOSAL_CREATED",),
                    "approved": (
                        "PATCH_PROPOSAL_CREATED",
                        "PATCH_PROPOSAL_APPROVED",
                    ),
                    "rejected": (
                        "PATCH_PROPOSAL_CREATED",
                        "PATCH_PROPOSAL_REJECTED",
                    ),
                    "applying": (
                        "PATCH_PROPOSAL_CREATED",
                        "PATCH_PROPOSAL_APPROVED",
                    ),
                    "applied": (
                        "PATCH_PROPOSAL_CREATED",
                        "PATCH_PROPOSAL_APPROVED",
                        "PATCH_PROPOSAL_APPLIED",
                    ),
                    "failed": ("PATCH_PROPOSAL_CREATED",),
                }.get(status)

                if expected is None:
                    warnings.append(
                        f"{proposal_id}: unrecognized proposal status {status!r}."
                    )
                    continue

                for expected_event in expected:
                    if expected_event not in event_names:
                        warnings.append(
                            f"{proposal_id}: missing audit event {expected_event}."
                        )

                if status == "applying":
                    linked = any(
                        row[0] == proposal_id
                        for row in self._transaction_proposal_pairs()
                    )
                    if not linked:
                        warnings.append(
                            f"{proposal_id}: applying proposal has no transaction "
                            "journal entry; manual review required."
                        )

            result_status = (
                "FAIL" if critical else "WARN" if warnings else "PASS"
            )

            return {
                "status": result_status,
                "message": (
                    f"Checked {transaction_count} transaction(s) and "
                    f"{len(proposals)} proposal file(s); "
                    f"{len(warnings)} warning(s), {len(critical)} critical issue(s)."
                ),
                "warnings": warnings,
                "critical_issues": critical,
                "read_only": True,
            }

        except (
            OSError, sqlite3.Error, ValueError, KeyError,
            TypeError, json.JSONDecodeError
        ) as error:
            return {
                "status": "FAIL",
                "message": f"Consistency check failed: {error}",
                "read_only": True,
            }

    def _transaction_proposal_pairs(self):
        """Return transaction/proposal links using a read-only connection."""
        path = self.root / "elon.sqlite3"
        uri = path.as_uri() + "?mode=ro"

        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            return db.execute(
                "SELECT transaction_id, proposal_id FROM patch_transactions"
            ).fetchall()

    def _directory_check(self):
        required = ("workspace", "backups", "logs")
        missing = [
            name for name in required
            if not (self.root / name).is_dir()
        ]

        if missing:
            return {
                "status": "FAIL",
                "message": "Missing required directories: " + ", ".join(missing),
            }

        return {
            "status": "PASS",
            "message": "All required directories exist.",
        }

    def run(self):
        checks = {
            "directories": self._directory_check(),
            "database": self._database_check(),
            "audit": self._audit_check(),
            "consistency": self._consistency_check(),
        }

        statuses = [item["status"] for item in checks.values()]
        overall = (
            "FAIL" if "FAIL" in statuses
            else "WARN" if "WARN" in statuses
            else "PASS"
        )

        return {
            "status": overall,
            "checks": checks,
            "read_only": True,
            "automatic_recovery": False,
            "ai_calls": False,
        }
