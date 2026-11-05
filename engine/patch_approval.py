import difflib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
import stat
from pathlib import Path

from security.policy import PolicyError


class PatchApprovalError(ValueError):
    pass


class PatchApprovalManager:
    MAX_PATCHES = 8
    MAX_PATCH_BYTES = 2_000_000
    ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")

    def __init__(self, engine):
        self.engine = engine
        self.directory = engine.workspace / "patch_proposals"
        self.directory.mkdir(parents=True, exist_ok=True)

        if self.directory.is_symlink():
            raise PatchApprovalError("Proposal directory cannot be a symlink")

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _digest(data):
        raw = json.dumps(
            data, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _path(self, proposal_id):
        if not isinstance(proposal_id, str):
            raise PatchApprovalError("Invalid proposal ID")

        if not self.ID_PATTERN.fullmatch(proposal_id):
            raise PatchApprovalError("Invalid proposal ID")

        path = self.directory / f"{proposal_id}.json"

        if path.is_symlink():
            raise PatchApprovalError("Proposal symlink rejected")

        return path

    @contextmanager
    def _proposal_lock(self, proposal_id):
        self._path(proposal_id)
        lock_path = self.directory / f".{proposal_id}.lock"

        flags = os.O_CREAT | os.O_RDWR
        if not hasattr(os, "O_NOFOLLOW"):
            raise PatchApprovalError("Safe lock-file support is unavailable")
        flags |= os.O_NOFOLLOW

        fd = None
        try:
            fd = os.open(lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PatchApprovalError("Proposal lock is not a regular file")

            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError as error:
            raise PatchApprovalError("Could not safely lock proposal") from error
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _immutable_data(self, record):
        return {
            "proposal_id": record["proposal_id"],
            "task_id": record["task_id"],
            "summary": record["summary"],
            "test_name": record["test_name"],
            "patches": record["patches"],
        }

    def _write(self, path, record):
        temporary = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=".proposal_",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump(record, file, sort_keys=True, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())

            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def _read(self, proposal_id):
        path = self._path(proposal_id)

        if not path.is_file():
            raise PatchApprovalError("Proposal does not exist")

        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise PatchApprovalError("Invalid proposal record") from error

        if not isinstance(record, dict):
            raise PatchApprovalError("Proposal record must be an object")

        required = {
            "proposal_id", "task_id", "summary", "test_name",
            "patches", "status", "created_at", "decided_at",
            "applied_at", "proposal_hash",
        }
        if not required.issubset(record):
            raise PatchApprovalError("Proposal is missing required fields")

        if record["proposal_id"] != proposal_id:
            raise PatchApprovalError("Proposal identity mismatch")

        task_id = record["task_id"]
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            raise PatchApprovalError("Invalid proposal task ID")

        if not isinstance(record["summary"], str) or len(record["summary"]) > 2000:
            raise PatchApprovalError("Invalid proposal summary")

        if not isinstance(record["test_name"], str):
            raise PatchApprovalError("Invalid registered test name")

        try:
            self.engine.policy.validate_test(record["test_name"])
        except Exception as error:
            raise PatchApprovalError("Proposal uses an unregistered test suite") from error

        if not isinstance(record["created_at"], str) or not record["created_at"]:
            raise PatchApprovalError("Invalid proposal creation timestamp")

        for field in ("decided_at", "applied_at"):
            value = record[field]
            if value is not None and (not isinstance(value, str) or not value):
                raise PatchApprovalError("Invalid proposal lifecycle timestamp")

        allowed_statuses = {
            "pending", "approved", "rejected", "applying", "applied", "failed"
        }
        if record["status"] not in allowed_statuses:
            raise PatchApprovalError("Invalid proposal status")

        status = record["status"]
        if status == "pending":
            if record["decided_at"] is not None or record["applied_at"] is not None:
                raise PatchApprovalError("Invalid pending proposal state")
        elif status in {"approved", "rejected", "applying", "failed"}:
            if not record["decided_at"] or record["applied_at"] is not None:
                raise PatchApprovalError("Invalid decided proposal state")
        elif status == "applied":
            if not record["decided_at"] or not record["applied_at"]:
                raise PatchApprovalError("Invalid applied proposal state")

        supplied_hash = record["proposal_hash"]
        if not isinstance(supplied_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", supplied_hash):
            raise PatchApprovalError("Invalid proposal integrity hash")

        try:
            actual_hash = self._digest(self._immutable_data(record))
        except Exception as error:
            raise PatchApprovalError("Invalid immutable proposal data") from error

        if supplied_hash != actual_hash:
            raise PatchApprovalError("Proposal integrity check failed")

        patches = record["patches"]
        if not isinstance(patches, list) or not 1 <= len(patches) <= self.MAX_PATCHES:
            raise PatchApprovalError("Invalid proposal patch set")

        seen = set()
        total_bytes = 0

        for patch in patches:
            if not isinstance(patch, dict):
                raise PatchApprovalError("Invalid patch entry")

            content = patch.get("content")
            path_value = patch.get("path")
            expected_hash = patch.get("expected_sha256")
            new_hash = patch.get("new_sha256")

            if not isinstance(content, str) or not isinstance(path_value, str):
                raise PatchApprovalError("Invalid patch content or path")

            if path_value in seen:
                raise PatchApprovalError("Duplicate patch path")
            seen.add(path_value)

            if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
                raise PatchApprovalError("Invalid original file hash")

            if not isinstance(new_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", new_hash):
                raise PatchApprovalError("Invalid replacement file hash")

            encoded = content.encode("utf-8")
            total_bytes += len(encoded)

            if not encoded.strip() or len(encoded) > self.MAX_PATCH_BYTES:
                raise PatchApprovalError("Invalid or oversized patch content")

            if hashlib.sha256(encoded).hexdigest() != new_hash:
                raise PatchApprovalError("Patch content integrity check failed")

            try:
                self.engine.policy.validate_modifiable_file(path_value)
            except Exception as error:
                raise PatchApprovalError("Patch path is no longer permitted: " + path_value) from error

        if total_bytes > self.MAX_PATCH_BYTES * self.MAX_PATCHES:
            raise PatchApprovalError("Total proposal size exceeded")

        if not self.engine.get_task(task_id):
            raise PatchApprovalError("Proposal task no longer exists")

        return record

    def save_proposal(self, task_id, patches, summary="", test_name="core"):
        task = self.engine.get_task(task_id)

        if not task:
            raise PatchApprovalError("Task does not exist")

        if task["status"] in ("completed", "cancelled"):
            raise PatchApprovalError("Task is not modifiable")

        if task["repair_attempts"] >= self.engine.policy.MAX_REPAIRS:
            raise PatchApprovalError("Maximum repair attempts exceeded")

        if not isinstance(summary, str) or len(summary) > 2000:
            raise PatchApprovalError("Invalid proposal summary")

        if not isinstance(patches, list) or not 1 <= len(patches) <= self.MAX_PATCHES:
            raise PatchApprovalError("Patch set must contain 1 to 8 files")

        try:
            self.engine.policy.validate_test(test_name)
        except PolicyError as error:
            raise PatchApprovalError(str(error)) from error

        prepared = []
        seen = set()

        for patch in patches:
            if not isinstance(patch, dict):
                raise PatchApprovalError("Invalid patch entry")

            path = patch.get("path")
            content = patch.get("content")
            expected = patch.get("expected_sha256")

            if not isinstance(path, str) or not isinstance(content, str):
                raise PatchApprovalError("Patch path and content must be text")

            if path in seen:
                raise PatchApprovalError("Duplicate patch paths are not allowed")
            seen.add(path)

            encoded = content.encode("utf-8")

            if not encoded.strip():
                raise PatchApprovalError("Empty patch rejected")

            if len(encoded) > self.MAX_PATCH_BYTES:
                raise PatchApprovalError("Patch exceeds 2 MB")

            try:
                target = self.engine.policy.validate_modifiable_file(path)
                original = target.read_bytes()
                original.decode("utf-8")
            except Exception as error:
                raise PatchApprovalError(
                    f"Cannot inspect proposed file: {path}"
                ) from error

            old_hash = hashlib.sha256(original).hexdigest()

            if expected != old_hash:
                raise PatchApprovalError(
                    f"File changed since inspection: {path}"
                )

            if original.decode("utf-8") == content:
                raise PatchApprovalError(f"No actual change proposed: {path}")

            prepared.append({
                "path": path,
                "expected_sha256": old_hash,
                "new_sha256": hashlib.sha256(encoded).hexdigest(),
                "content": content,
            })

        proposal_id = uuid.uuid4().hex

        record = {
            "proposal_id": proposal_id,
            "task_id": task_id,
            "summary": summary,
            "test_name": test_name,
            "patches": prepared,
            "status": "pending",
            "created_at": self._now(),
            "decided_at": None,
            "applied_at": None,
        }

        record["proposal_hash"] = self._digest(
            self._immutable_data(record)
        )

        self._write(self._path(proposal_id), record)

        self.engine.audit.append(
            "PATCH_PROPOSAL_CREATED",
            {
                "proposal_id": proposal_id,
                "task_id": task_id,
                "patch_hash": record["proposal_hash"],
                "files": len(prepared),
            },
        )

        return record

    def preview_diff(self, proposal_id):
        """Return read-only unified diffs for a proposal."""
        record = self._read(proposal_id)
        previews = []

        for patch in record["patches"]:
            try:
                target = self.engine.policy.validate_modifiable_file(
                    patch["path"]
                )
                original_bytes = target.read_bytes()
                original_hash = hashlib.sha256(original_bytes).hexdigest()

                if original_hash != patch["expected_sha256"]:
                    raise PatchApprovalError(
                        "Source changed since proposal creation: "
                        + patch["path"]
                    )

                original = original_bytes.decode("utf-8")
            except PatchApprovalError:
                raise
            except Exception as error:
                raise PatchApprovalError(
                    "Cannot safely preview source file: " + patch["path"]
                ) from error

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                patch["content"].splitlines(keepends=True),
                fromfile="a/" + patch["path"],
                tofile="b/" + patch["path"],
            ))

            previews.append({
                "path": patch["path"],
                "diff": diff,
            })

        return previews

    def get_proposal(self, proposal_id):
        return self._read(proposal_id)

    def list_proposals(self):
        results = []

        for path in sorted(self.directory.glob("*.json")):
            if path.is_symlink():
                continue

            proposal_id = path.stem
            record = self._read(proposal_id)
            results.append(record)

        return results

    def _decide(self, proposal_id, decision):
        with self._proposal_lock(proposal_id):
            record = self._read(proposal_id)

            if record["status"] != "pending":
                raise PatchApprovalError("Proposal is no longer pending")

            record["status"] = decision
            record["decided_at"] = self._now()
            self._write(self._path(proposal_id), record)

            self.engine.audit.append(
                "PATCH_PROPOSAL_" + decision.upper(),
                {
                    "proposal_id": proposal_id,
                    "task_id": record["task_id"],
                    "patch_hash": record["proposal_hash"],
                },
            )
            return record

    def approve(self, proposal_id):
        return self._decide(proposal_id, "approved")

    def reject(self, proposal_id):
        return self._decide(proposal_id, "rejected")

    def mark_applying(self, proposal_id):
        with self._proposal_lock(proposal_id):
            record = self._read(proposal_id)

            if record["status"] != "approved":
                raise PatchApprovalError("Only approved proposals can be applied")

            record["status"] = "applying"
            self._write(self._path(proposal_id), record)
            return record

    def apply_approved(self, proposal_id, transaction):
        record = self._read(proposal_id)

        if record["status"] != "approved":
            raise PatchApprovalError("Only approved proposals can be applied")

        task = self.engine.get_task(record["task_id"])
        if not task or task["status"] in ("completed", "cancelled"):
            raise PatchApprovalError("Task is no longer modifiable")

        if task["repair_attempts"] >= self.engine.policy.MAX_REPAIRS:
            raise PatchApprovalError("Maximum repair attempts exceeded")

        # Recheck every source file immediately before applying.
        for patch in record["patches"]:
            try:
                target = self.engine.policy.validate_modifiable_file(
                    patch["path"]
                )
                current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            except Exception as error:
                raise PatchApprovalError(
                    "Cannot revalidate source file: " + patch["path"]
                ) from error

            if current_hash != patch["expected_sha256"]:
                raise PatchApprovalError(
                    "Source changed after approval: " + patch["path"]
                )

        record = self.mark_applying(proposal_id)

        transaction_patches = [
            {
                "path": patch["path"],
                "content": patch["content"],
                "expected_sha256": patch["expected_sha256"],
            }
            for patch in record["patches"]
        ]

        applied = []

        try:
            applied = transaction.apply(
                record["task_id"], transaction_patches, proposal_id=proposal_id
            )

            test_result = self.engine.run_registered_tests(
                record["test_name"]
            )

            if not test_result.get("success"):
                rollback_errors = transaction.rollback(applied)

                self.engine.audit.append(
                    "PATCH_PROPOSAL_TEST_FAILED",
                    {
                        "proposal_id": proposal_id,
                        "task_id": record["task_id"],
                        "rollback_errors": rollback_errors,
                    },
                )

                self.mark_failed(proposal_id)

                if rollback_errors:
                    raise PatchApprovalError(
                        "Tests failed and rollback was incomplete: "
                        + "; ".join(rollback_errors)
                    )

                return {
                    "success": False,
                    "status": "failed",
                    "tests": test_result,
                    "rollback": "complete",
                }

            self.mark_applied(proposal_id)

            return {
                "success": True,
                "status": "applied",
                "patches": applied,
                "tests": test_result,
            }

        except Exception:
            current = self._read(proposal_id)

            if current["status"] == "applying":
                if applied:
                    rollback_errors = transaction.rollback(applied)
                    if rollback_errors:
                        self.engine.audit.append(
                            "PATCH_PROPOSAL_ROLLBACK_INCOMPLETE",
                            {
                                "proposal_id": proposal_id,
                                "errors": rollback_errors,
                            },
                        )

                self.mark_failed(proposal_id)

            raise

    def mark_applied(self, proposal_id):
        with self._proposal_lock(proposal_id):
            record = self._read(proposal_id)

            if record["status"] != "applying":
                raise PatchApprovalError("Proposal is not being applied")

            record["status"] = "applied"
            record["applied_at"] = self._now()
            self._write(self._path(proposal_id), record)

            self.engine.audit.append(
                "PATCH_PROPOSAL_APPLIED",
                {
                    "proposal_id": proposal_id,
                    "task_id": record["task_id"],
                    "patch_hash": record["proposal_hash"],
                },
            )
            return record

    def reconcile_transaction(self, transaction_id):
        if not isinstance(transaction_id, str) or not self.ID_PATTERN.fullmatch(transaction_id):
            raise PatchApprovalError("Invalid transaction ID")

        with self.engine._connect() as db:
            row = db.execute(
                """SELECT proposal_id, status, task_id
                   FROM patch_transactions WHERE transaction_id = ?""",
                (transaction_id,),
            ).fetchone()

        if row is None:
            raise PatchApprovalError("Patch transaction does not exist")

        proposal_id, transaction_status, task_id = row

        if proposal_id is None:
            return {
                "action": "no_linked_proposal",
                "transaction_id": transaction_id,
                "transaction_status": transaction_status,
            }

        if transaction_status in {"applying", "recovery_required"}:
            return {
                "action": "manual_recovery_required",
                "transaction_id": transaction_id,
                "proposal_id": proposal_id,
                "transaction_status": transaction_status,
            }

        if transaction_status == "applied":
            return {
                "action": "application_committed_tests_unknown",
                "transaction_id": transaction_id,
                "proposal_id": proposal_id,
                "transaction_status": transaction_status,
            }

        if transaction_status != "rolled_back":
            return {
                "action": "no_action",
                "transaction_id": transaction_id,
                "proposal_id": proposal_id,
                "transaction_status": transaction_status,
            }

        with self._proposal_lock(proposal_id):
            record = self._read(proposal_id)

            if record["task_id"] != task_id:
                raise PatchApprovalError("Transaction and proposal task IDs do not match")

            if record["status"] == "applying":
                record["status"] = "failed"
                self._write(self._path(proposal_id), record)
            elif record["status"] != "failed":
                raise PatchApprovalError(
                    "Proposal is not in an eligible reconciliation state"
                )

            # Audit append is separate from the proposal-file write.
            # Check existing records so a retry after interruption is idempotent.
            audit_path = self.engine.audit.path
            audit_exists = False

            if audit_path.exists():
                for line in audit_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    details = event.get("details", {})
                    if (
                        event.get("event") == "PATCH_PROPOSAL_RECONCILED_FAILED"
                        and details.get("transaction_id") == transaction_id
                    ):
                        audit_exists = True
                        break

            if not audit_exists:
                self.engine.audit.append(
                    "PATCH_PROPOSAL_RECONCILED_FAILED",
                    {
                        "transaction_id": transaction_id,
                        "proposal_id": proposal_id,
                        "task_id": task_id,
                        "reason": "transaction_rolled_back",
                    },
                )

        return {
            "action": "reconciled_failed",
            "transaction_id": transaction_id,
            "proposal_id": proposal_id,
            "transaction_status": transaction_status,
            "proposal_status": "failed",
        }

    def mark_failed(self, proposal_id):
        with self._proposal_lock(proposal_id):
            record = self._read(proposal_id)

            if record["status"] != "applying":
                raise PatchApprovalError("Proposal is not being applied")

            record["status"] = "failed"
            self._write(self._path(proposal_id), record)
            return record
