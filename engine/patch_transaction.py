import hashlib
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from security.policy import PolicyError


class PatchTransaction:
    MAX_PATCHES = 8
    MAX_PATCH_BYTES = 2_000_000

    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _hash(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _set_transaction_status(self, transaction_id, status, error=None):
        with self.engine._connect() as db:
            db.execute(
                """UPDATE patch_transactions
                   SET status = ?, error = ?, updated_at = ?
                   WHERE transaction_id = ?""",
                (status, error, self._now(), transaction_id),
            )

    def apply(self, task_id, patches, proposal_id=None):
        if proposal_id is not None:
            valid_id = (
                isinstance(proposal_id, str)
                and len(proposal_id) == 32
                and all(c in "0123456789abcdef" for c in proposal_id)
            )
            if not valid_id:
                raise PolicyError("Invalid proposal ID")

        engine = self.engine
        task = engine.get_task(task_id)

        if not task:
            raise ValueError("Task does not exist")

        if task["status"] in ("completed", "cancelled"):
            raise PolicyError("Task is not modifiable")

        if task["repair_attempts"] >= engine.policy.MAX_REPAIRS:
            raise PolicyError("Maximum repair attempts exceeded")

        if not isinstance(patches, list):
            raise PolicyError("Patch set must be a list")

        if not 1 <= len(patches) <= self.MAX_PATCHES:
            raise PolicyError("Patch set must contain 1 to 8 files")

        prepared = []
        seen = set()

        for patch in patches:
            if not isinstance(patch, dict):
                raise PolicyError("Invalid patch entry")

            path = patch.get("path")
            content = patch.get("content")

            if not isinstance(path, str) or not isinstance(content, str):
                raise PolicyError("Patch path and content must be text")

            if path in seen:
                raise PolicyError("Duplicate paths are not allowed")
            seen.add(path)

            encoded = content.encode("utf-8")

            if not encoded.strip():
                raise PolicyError("Empty patch rejected")

            if len(encoded) > self.MAX_PATCH_BYTES:
                raise PolicyError("Patch exceeds 2 MB")

            target = engine.policy.validate_modifiable_file(path)
            original = target.read_bytes()
            old_hash = self._hash(original)

            if patch.get("expected_sha256") != old_hash:
                raise PolicyError("File changed since inspection: " + path)

            if original.decode("utf-8") == content:
                raise PolicyError("No actual change proposed: " + path)

            prepared.append({
                "path": path,
                "target": target,
                "content": encoded,
                "old_hash": old_hash,
                "old_mode": target.stat().st_mode,
            })

        backup_items = []
        staged = []
        applied = []
        transaction_id = None
        journal_created = False

        try:
            # Create and verify every backup before modifying files.
            for item in prepared:
                backup_name = (
                    f"task_{task_id}_{item['target'].name}_"
                    f"{item['old_hash'][:12]}_{uuid.uuid4().hex[:8]}.bak"
                )
                backup = engine.backups / backup_name

                with open(backup, "xb") as file:
                    file.write(item["target"].read_bytes())

                if self._hash(backup.read_bytes()) != item["old_hash"]:
                    raise PolicyError("Backup verification failed")

                item["backup"] = backup
                backup_items.append(item)

            # Stage all replacement files first.
            for item in prepared:
                temporary = None

                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=item["target"].parent,
                    prefix=".elon_batch_",
                    delete=False,
                ) as file:
                    temporary = Path(file.name)
                    file.write(item["content"])

                os.chmod(temporary, item["old_mode"])
                item["temporary"] = temporary
                staged.append(item)

            # Recheck every target immediately before replacement.
            for item in prepared:
                current = item["target"].read_bytes()
                if self._hash(current) != item["old_hash"]:
                    raise PolicyError(
                        "File changed during transaction: " + item["path"]
                    )

            # Persist transaction before the first file replacement.
            transaction_id = uuid.uuid4().hex
            now = self._now()

            with engine._connect() as db:
                db.execute(
                    """INSERT INTO patch_transactions
                       (transaction_id, task_id, proposal_id, status,
                        repair_counted, created_at, updated_at, error)
                       VALUES (?, ?, ?, 'applying', 0, ?, ?, NULL)""",
                    (transaction_id, task_id, proposal_id, now, now),
                )
                db.executemany(
                    """INSERT INTO patch_transaction_files
                       (transaction_id, ordinal, path, backup,
                        old_sha256, new_sha256, old_mode, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    [
                        (
                            transaction_id,
                            index,
                            item["path"],
                            item["backup"].name,
                            item["old_hash"],
                            self._hash(item["content"]),
                            item["old_mode"],
                        )
                        for index, item in enumerate(staged)
                    ],
                )

            journal_created = True

            # Replace files one by one and record each transition.
            for index, item in enumerate(staged):
                with engine._connect() as db:
                    db.execute(
                        """UPDATE patch_transaction_files
                           SET state = 'replacing'
                           WHERE transaction_id = ? AND ordinal = ?""",
                        (transaction_id, index),
                    )

                if self._hash(item["target"].read_bytes()) != item["old_hash"]:
                    raise PolicyError(
                        "File changed immediately before replacement: "
                        + item["path"]
                    )

                os.replace(item["temporary"], item["target"])
                item["transaction_id"] = transaction_id
                applied.append(item)

                with engine._connect() as db:
                    db.execute(
                        """UPDATE patch_transaction_files
                           SET state = 'replaced'
                           WHERE transaction_id = ? AND ordinal = ?""",
                        (transaction_id, index),
                    )

            # Complete journal and count the repair exactly once.
            with engine._connect() as db:
                cursor = db.execute(
                    """UPDATE patch_transactions
                       SET status = 'applied', repair_counted = 1,
                           updated_at = ?
                       WHERE transaction_id = ?
                         AND status = 'applying'
                         AND repair_counted = 0""",
                    (self._now(), transaction_id),
                )
                if cursor.rowcount != 1:
                    raise PolicyError("Transaction journal state conflict")

                cursor = db.execute(
                    """UPDATE tasks
                       SET repair_attempts = repair_attempts + 1
                       WHERE id = ?""",
                    (task_id,),
                )
                if cursor.rowcount != 1:
                    raise PolicyError("Task disappeared during patch transaction")

            for item in applied:
                engine.audit.append(
                    "PATCH_APPLIED",
                    {
                        "task_id": task_id,
                        "transaction_id": transaction_id,
                        "path": item["path"],
                        "old_hash": item["old_hash"],
                        "new_hash": self._hash(item["target"].read_bytes()),
                        "backup": item["backup"].name,
                        "repair": True,
                        "batch": True,
                    },
                )

            engine.audit.append(
                "PATCH_SET_APPLIED",
                {
                    "task_id": task_id,
                    "transaction_id": transaction_id,
                    "files": len(applied),
                },
            )

            return [
                {
                    "path": item["path"],
                    "transaction_id": transaction_id,
                    "backup": str(item["backup"]),
                    "old_sha256": item["old_hash"],
                    "old_hash": item["old_hash"],
                    "new_sha256": self._hash(
                        item["target"].read_bytes()
                    ),
                    "content": item["content"],
                    "old_mode": item["old_mode"],
                }
                for item in applied
            ]

        except Exception as error:
            rollback_errors = self.rollback(applied)

            if journal_created:
                try:
                    status = (
                        "recovery_required"
                        if rollback_errors
                        else "rolled_back"
                    )
                    self._set_transaction_status(
                        transaction_id, status, str(error)
                    )
                except Exception as journal_error:
                    rollback_errors.append(
                        f"Journal update failed: {journal_error}"
                    )

            if rollback_errors:
                engine.audit.append(
                    "PATCH_SET_ROLLBACK_INCOMPLETE",
                    {
                        "task_id": task_id,
                        "errors": rollback_errors,
                    },
                )

            raise

        finally:
            for item in staged:
                temporary = item.get("temporary")
                if temporary and temporary.exists():
                    temporary.unlink()

    def rollback(self, applied):
        errors = []
        engine = self.engine

        for item in reversed(applied):
            try:
                target = engine.policy.validate_modifiable_file(
                    item["path"]
                )

                current_hash = self._hash(target.read_bytes())
                new_hash = self._hash(item["content"])

                if current_hash != new_hash:
                    raise PolicyError(
                        "Rollback skipped; file changed: " + item["path"]
                    )

                backup = Path(item["backup"]).resolve(strict=True)
                backup.relative_to(engine.backups.resolve())

                if backup.is_symlink() or not backup.is_file():
                    raise PolicyError("Invalid backup file")

                original = backup.read_bytes()

                if self._hash(original) != item["old_hash"]:
                    raise PolicyError("Backup integrity check failed")

                temporary = None

                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=target.parent,
                        prefix=".elon_rollback_",
                        delete=False,
                    ) as file:
                        temporary = Path(file.name)
                        file.write(original)

                    os.chmod(temporary, item["old_mode"])
                    os.replace(temporary, target)

                    transaction_id = item.get("transaction_id")
                    if transaction_id:
                        with engine._connect() as db:
                            cursor = db.execute(
                                """UPDATE patch_transaction_files
                                   SET state = 'rolled_back'
                                   WHERE transaction_id = ? AND path = ?""",
                                (transaction_id, item["path"]),
                            )
                            if cursor.rowcount != 1:
                                raise PolicyError(
                                    "Rollback journal file state conflict"
                                )

                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()

                engine.audit.append(
                    "PATCH_ROLLED_BACK",
                    {
                        "path": item["path"],
                        "restored_hash": item["old_hash"],
                        "batch": True,
                    },
                )

            except Exception as error:
                errors.append(f"{item['path']}: {error}")

        transaction_ids = {
            item.get("transaction_id")
            for item in applied
            if item.get("transaction_id")
        }

        for transaction_id in transaction_ids:
            try:
                status = "recovery_required" if errors else "rolled_back"
                self._set_transaction_status(
                    transaction_id,
                    status,
                    "; ".join(errors) if errors else None,
                )
            except Exception as error:
                errors.append(
                    f"Transaction journal update failed: {error}"
                )

        return errors


    def recover(self, transaction_id, confirm_stopped=False):
        """Safely roll back an interrupted patch transaction."""
        import re

        engine = self.engine

        if confirm_stopped is not True:
            raise PolicyError(
                "Explicit confirmation required: original process must be stopped."
            )

        if (
            not isinstance(transaction_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
        ):
            raise PolicyError("Invalid transaction ID")

        with engine._connect() as db:
            transaction = db.execute(
                """SELECT task_id, status, repair_counted
                   FROM patch_transactions
                   WHERE transaction_id = ?""",
                (transaction_id,),
            ).fetchone()

            rows = db.execute(
                """SELECT ordinal, path, backup, old_sha256,
                          new_sha256, old_mode, state
                   FROM patch_transaction_files
                   WHERE transaction_id = ?
                   ORDER BY ordinal""",
                (transaction_id,),
            ).fetchall()

        if transaction is None:
            raise PolicyError("Patch transaction does not exist")

        task_id, status, repair_counted = transaction

        if status not in ("applying", "recovery_required"):
            raise PolicyError(
                "Only applying or recovery_required transactions can be recovered"
            )

        if not rows or len(rows) > self.MAX_PATCHES:
            raise PolicyError("Invalid transaction file records")

        if [row[0] for row in rows] != list(range(len(rows))):
            raise PolicyError("Transaction file order is invalid")

        backup_root_path = engine.backups

        if backup_root_path.is_symlink() or not backup_root_path.is_dir():
            raise PolicyError("Backup directory is missing or unsafe")

        backup_root = backup_root_path.resolve(strict=True)
        validated = []

        # Validate EVERY target and backup before restoring ANY file.
        for ordinal, relative_path, backup_name, old_hash, new_hash, old_mode, state in rows:
            if (
                not isinstance(relative_path, str)
                or not isinstance(backup_name, str)
                or Path(backup_name).name != backup_name
                or backup_name in ("", ".", "..")
            ):
                raise PolicyError("Unsafe transaction path or backup name")

            for digest in (old_hash, new_hash):
                if (
                    not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                ):
                    raise PolicyError("Invalid transaction hash")

            if old_hash == new_hash:
                raise PolicyError("Invalid transaction hash pair")

            if (
                isinstance(old_mode, bool)
                or not isinstance(old_mode, int)
                or not stat.S_ISREG(old_mode)
            ):
                raise PolicyError("Invalid original file mode")

            target = engine.policy.validate_modifiable_file(relative_path)
            current_hash = self._hash(target.read_bytes())

            if current_hash not in (old_hash, new_hash):
                raise PolicyError(
                    "Recovery refused; file has an unexpected hash: "
                    + relative_path
                )

            backup_path = backup_root_path / backup_name

            if backup_path.is_symlink():
                raise PolicyError("Backup symlink rejected: " + backup_name)

            try:
                backup_stat = backup_path.lstat()
                resolved_backup = backup_path.resolve(strict=True)
            except OSError:
                raise PolicyError(
                    "Backup is missing or unreadable: " + backup_name
                ) from None

            if (
                not stat.S_ISREG(backup_stat.st_mode)
                or resolved_backup.parent != backup_root
            ):
                raise PolicyError("Unsafe backup file: " + backup_name)

            backup_data = backup_path.read_bytes()

            if self._hash(backup_data) != old_hash:
                raise PolicyError(
                    "Backup integrity check failed: " + backup_name
                )

            validated.append({
                "ordinal": ordinal,
                "path": relative_path,
                "target": target,
                "backup": backup_path,
                "backup_data": backup_data,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "old_mode": old_mode,
                "current_hash": current_hash,
            })

        restored = []

        try:
            # Recheck each file immediately before restoring it.
            for item in reversed(validated):
                target = engine.policy.validate_modifiable_file(item["path"])
                current_hash = self._hash(target.read_bytes())

                if current_hash == item["old_hash"]:
                    # Already restored; makes recovery idempotent.
                    pass
                elif current_hash == item["new_hash"]:
                    temporary = None

                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="wb",
                            dir=target.parent,
                            prefix=".elon_recovery_",
                            delete=False,
                        ) as file:
                            temporary = Path(file.name)
                            file.write(item["backup_data"])
                            file.flush()
                            os.fsync(file.fileno())

                        os.chmod(temporary, item["old_mode"])

                        if self._hash(temporary.read_bytes()) != item["old_hash"]:
                            raise PolicyError("Recovery staging verification failed")

                        # Final check immediately before atomic replacement.
                        if self._hash(target.read_bytes()) != item["new_hash"]:
                            raise PolicyError(
                                "File changed during recovery: " + item["path"]
                            )

                        os.replace(temporary, target)
                        temporary = None
                        restored.append(item["path"])

                    finally:
                        if temporary and temporary.exists():
                            temporary.unlink()
                else:
                    raise PolicyError(
                        "Recovery refused; file changed during recovery: "
                        + item["path"]
                    )

                # A crash here is safe: next recovery checks actual file hashes.
                with engine._connect() as db:
                    cursor = db.execute(
                        """UPDATE patch_transaction_files
                           SET state = 'rolled_back'
                           WHERE transaction_id = ? AND ordinal = ?""",
                        (transaction_id, item["ordinal"]),
                    )
                    if cursor.rowcount != 1:
                        raise PolicyError("Recovery journal file update failed")

            with engine._connect() as db:
                cursor = db.execute(
                    """UPDATE patch_transactions
                       SET status = 'rolled_back', updated_at = ?, error = NULL
                       WHERE transaction_id = ?
                         AND status IN ('applying', 'recovery_required')""",
                    (self._now(), transaction_id),
                )
                if cursor.rowcount != 1:
                    raise PolicyError("Recovery transaction state conflict")

            engine.audit.append(
                "PATCH_TRANSACTION_RECOVERED",
                {
                    "transaction_id": transaction_id,
                    "task_id": task_id,
                    "files": len(validated),
                    "restored_files": restored,
                    "repair_counted": repair_counted,
                    "repair_attempts_incremented": False,
                },
            )

            return {
                "transaction_id": transaction_id,
                "task_id": task_id,
                "status": "rolled_back",
                "files_checked": len(validated),
                "files_restored": len(restored),
                "repair_counted": repair_counted,
            }

        except Exception as error:
            try:
                self._set_transaction_status(
                    transaction_id,
                    "recovery_required",
                    str(error),
                )
                engine.audit.append(
                    "PATCH_TRANSACTION_RECOVERY_INCOMPLETE",
                    {
                        "transaction_id": transaction_id,
                        "task_id": task_id,
                        "error": str(error),
                        "files_restored_before_error": restored,
                    },
                )
            except Exception:
                pass
            raise
