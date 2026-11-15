import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.audit import AuditLog
from engine.health_check import HealthChecker


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        for name in ("workspace", "backups", "logs"):
            (self.root / name).mkdir()

        with closing(sqlite3.connect(self.root / "elon.sqlite3")) as db:
            db.execute("""
                CREATE TABLE patch_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    status TEXT
                )
            """)

    def tearDown(self):
        self.temp.cleanup()

    def test_healthy_project_passes(self):
        report = HealthChecker(self.root).run()
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["read_only"])

    def test_missing_directory_fails(self):
        (self.root / "workspace").rmdir()
        report = HealthChecker(self.root).run()
        self.assertEqual(report["checks"]["directories"]["status"], "FAIL")
        self.assertEqual(report["status"], "FAIL")

    def test_missing_database_fails(self):
        (self.root / "elon.sqlite3").unlink()
        report = HealthChecker(self.root).run()
        self.assertEqual(report["checks"]["database"]["status"], "FAIL")

    def test_unfinished_patch_warns(self):
        with closing(sqlite3.connect(self.root / "elon.sqlite3")) as db:
            db.execute(
                "INSERT INTO patch_transactions VALUES (?, ?)",
                ("test-transaction", "recovery_required"),
            )
            db.commit()

        report = HealthChecker(self.root).run()
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(
            report["checks"]["database"]["unfinished_transactions"][0]["status"],
            "recovery_required",
        )

    def test_pending_recovery_outbox_warns_with_minimal_schema(self):
        with closing(sqlite3.connect(self.root / "elon.sqlite3")) as db:
            db.execute(
                """CREATE TABLE patch_recovery_audit_outbox (
                    transaction_id TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                )"""
            )
            db.execute(
                """INSERT INTO patch_recovery_audit_outbox
                   (transaction_id, task_id, details_json, created_at, delivered_at)
                   VALUES (?, ?, ?, ?, NULL)""",
                ("pending-transaction", 1, "{}", "now"),
            )
            db.commit()

        report = HealthChecker(self.root).run()
        consistency = report["checks"]["consistency"]
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(consistency["status"], "WARN")
        self.assertTrue(
            any("outbox event(s) pending delivery" in item
                for item in consistency["warnings"])
        )

    def test_corrupt_audit_fails(self):
        path = self.root / "logs" / "audit.jsonl"
        path.write_text("{broken json\n", encoding="utf-8")
        report = HealthChecker(self.root).run()
        self.assertEqual(report["checks"]["audit"]["status"], "FAIL")

    def test_valid_audit_passes(self):
        audit = AuditLog(self.root / "logs" / "audit.jsonl")
        audit.append("test", {"safe": True})
        report = HealthChecker(self.root).run()
        self.assertEqual(report["checks"]["audit"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
