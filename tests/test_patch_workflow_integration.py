import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from engine.task_engine import TaskEngine
from engine.patch_approval import PatchApprovalManager, PatchApprovalError
from engine.patch_transaction import PatchTransaction
from engine.patch_proposal_service import PatchProposalService


class PatchWorkflowIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.target = self.engine.workspace / "sample.py"
        self.target.write_text("value = 10\n", encoding="utf-8")
        self.task_id = self.engine.create_task(
            "Isolated patch workflow integration test"
        )
        self.manager = PatchApprovalManager(self.engine)
        self.transaction = PatchTransaction(self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def make_patch(self):
        original = self.target.read_bytes()
        return [{
            "path": "sample.py",
            "expected_sha256": hashlib.sha256(original).hexdigest(),
            "content": "value = 20\n",
        }]

    def test_complete_approved_patch_workflow(self):
        proposal = self.manager.save_proposal(
            self.task_id,
            self.make_patch(),
            summary="Integration test patch",
            test_name="core",
        )

        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.target.read_text(), "value = 10\n")

        approved = self.manager.approve(proposal["proposal_id"])
        self.assertEqual(approved["status"], "approved")

        self.engine.run_registered_tests = Mock(
            return_value={"success": True, "returncode": 0}
        )

        result = self.manager.apply_approved(
            proposal["proposal_id"], self.transaction
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.target.read_text(), "value = 20\n")
        self.assertEqual(
            self.manager.get_proposal(proposal["proposal_id"])["status"],
            "applied",
        )
        self.assertTrue(self.engine.audit.verify())

    def test_transaction_journal_persists_proposal_id(self):
        proposal = self.manager.save_proposal(
            self.task_id,
            self.make_patch(),
            summary="Proposal ID persistence test",
            test_name="core",
        )
        proposal_id = proposal["proposal_id"]

        self.manager.approve(proposal_id)
        self.engine.run_registered_tests = Mock(
            return_value={"success": True, "returncode": 0}
        )

        result = self.manager.apply_approved(
            proposal_id, self.transaction
        )

        self.assertTrue(result["success"])

        with self.engine._connect() as db:
            row = db.execute(
                "SELECT proposal_id FROM patch_transactions WHERE task_id = ?",
                (self.task_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], proposal_id)

    def test_failed_tests_restore_original_file(self):
        proposal = self.manager.save_proposal(
            self.task_id,
            self.make_patch(),
            summary="Integration rollback test",
            test_name="core",
        )
        self.manager.approve(proposal["proposal_id"])

        self.engine.run_registered_tests = Mock(
            return_value={
                "success": False,
                "returncode": 1,
                "stderr": "Simulated test failure",
            }
        )

        result = self.manager.apply_approved(
            proposal["proposal_id"], self.transaction
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rollback"], "complete")
        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertEqual(
            self.manager.get_proposal(proposal["proposal_id"])["status"],
            "failed",
        )
        self.assertTrue(self.engine.audit.verify())

    def test_full_repair_proposal_approval_and_rollback_flow(self):
        provider = Mock()
        provider.configured.return_value = True
        provider.model = "mock-e2e-provider"
        provider.propose_patch.return_value = {
            "action": "patch",
            "summary": "Fix simulated test failure",
            "path": "sample.py",
            "content": "value = 20\n",
        }

        service = PatchProposalService(self.engine, provider=provider)
        service.repairer._collect_context = Mock(
            return_value={"sample.py": "value = 10\n"}
        )

        # First failure: proposal generation. Second failure: post-apply test.
        self.engine.run_registered_tests = Mock(side_effect=[
            {"success": False, "returncode": 1, "stderr": "Initial failure"},
            {"success": False, "returncode": 1, "stderr": "Still failing"},
        ])

        generated = service.generate_repair(self.task_id, test_name="core")
        self.assertTrue(generated["success"])

        proposal = generated["proposal"]
        proposal_id = proposal["proposal_id"]

        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertEqual(self.engine.get_task(self.task_id)["repair_attempts"], 0)

        # Applying without approval must be rejected.
        with self.assertRaises(PatchApprovalError):
            self.manager.apply_approved(proposal_id, self.transaction)

        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"], "pending"
        )

        # Explicit approval, followed by apply and failed-test rollback.
        self.manager.approve(proposal_id)
        result = self.manager.apply_approved(proposal_id, self.transaction)

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rollback"], "complete")
        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"], "failed"
        )
        self.assertEqual(self.engine.get_task(self.task_id)["repair_attempts"], 1)
        self.assertEqual(self.engine.run_registered_tests.call_count, 2)
        provider.propose_patch.assert_called_once()
        self.assertTrue(self.engine.audit.verify())


    def _create_reconciliation_case(self, transaction_status):
        import uuid

        proposal = self.manager.save_proposal(
            self.task_id,
            self.make_patch(),
            summary="Reconciliation integration test",
            test_name="core",
        )
        proposal_id = proposal["proposal_id"]

        self.manager.approve(proposal_id)
        self.manager.mark_applying(proposal_id)

        transaction_id = uuid.uuid4().hex
        now = self.transaction._now()

        with self.engine._connect() as db:
            db.execute(
                """INSERT INTO patch_transactions
                   (transaction_id, task_id, proposal_id, status,
                    repair_counted, created_at, updated_at, error)
                   VALUES (?, ?, ?, ?, 0, ?, ?, NULL)""",
                (
                    transaction_id,
                    self.task_id,
                    proposal_id,
                    transaction_status,
                    now,
                    now,
                ),
            )

        return transaction_id, proposal_id

    def test_reconcile_rolled_back_transaction(self):
        transaction_id, proposal_id = self._create_reconciliation_case(
            "rolled_back"
        )

        result = self.manager.reconcile_transaction(transaction_id)

        self.assertEqual(result["action"], "reconciled_failed")
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"], "failed"
        )
        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertTrue(self.engine.audit.verify())

    def test_reconciliation_retry_is_idempotent(self):
        transaction_id, proposal_id = self._create_reconciliation_case(
            "rolled_back"
        )

        first = self.manager.reconcile_transaction(transaction_id)
        self.assertEqual(first["action"], "reconciled_failed")

        audit_path = self.engine.audit.path
        before = sum(
            1 for line in audit_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "PATCH_PROPOSAL_RECONCILED_FAILED"
        )

        second = self.manager.reconcile_transaction(transaction_id)

        after = sum(
            1 for line in audit_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "PATCH_PROPOSAL_RECONCILED_FAILED"
        )

        self.assertEqual(second["action"], "reconciled_failed")
        self.assertEqual(before, 1)
        self.assertEqual(after, 1)
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"], "failed"
        )
        self.assertTrue(self.engine.audit.verify())

    def test_applied_transaction_remains_unresolved(self):
        transaction_id, proposal_id = self._create_reconciliation_case(
            "applied"
        )

        result = self.manager.reconcile_transaction(transaction_id)

        self.assertEqual(
            result["action"], "application_committed_tests_unknown"
        )
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"], "applying"
        )

    def test_interrupted_transaction_requires_manual_recovery(self):
        transaction_id, proposal_id = self._create_reconciliation_case(
            "applying"
        )

        result = self.manager.reconcile_transaction(transaction_id)

        self.assertEqual(result["action"], "manual_recovery_required")
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"], "applying"
        )


if __name__ == "__main__":
    unittest.main()
