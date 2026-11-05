import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.patch_transaction import PatchTransaction
from engine.patch_approval import (
    PatchApprovalManager,
    PatchApprovalError,
)


class PatchApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.manager = PatchApprovalManager(self.engine)

        self.target = self.root / "workspace" / "sample.py"
        self.target.write_text("value = 10\n", encoding="utf-8")

        self.task_id = self.engine.create_task("Approval test")

    def tearDown(self):
        self.temp.cleanup()

    def make_patch(self):
        return [{
            "path": "sample.py",
            "content": "value = 20\n",
            "expected_sha256": hashlib.sha256(
                self.target.read_bytes()
            ).hexdigest(),
        }]

    def test_save_creates_pending_proposal_without_modification(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch(), "Change value"
        )
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["proposal_hash"])
        self.assertEqual(self.target.read_text(), "value = 10\n")

    def test_approve_and_reject(self):
        first = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.assertEqual(
            self.manager.approve(first["proposal_id"])["status"],
            "approved",
        )

        second = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.assertEqual(
            self.manager.reject(second["proposal_id"])["status"],
            "rejected",
        )
        self.assertTrue(self.engine.audit.verify())

    def test_cannot_decide_twice(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.manager.approve(record["proposal_id"])

        with self.assertRaises(PatchApprovalError):
            self.manager.reject(record["proposal_id"])

    def test_tampered_content_rejected(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        path = self.manager.directory / f"{record['proposal_id']}.json"
        data = json.loads(path.read_text())
        data["patches"][0]["content"] = "value = 999\n"
        path.write_text(json.dumps(data))

        with self.assertRaises(PatchApprovalError):
            self.manager.get_proposal(record["proposal_id"])

    def test_stale_source_rejected(self):
        patches = self.make_patch()
        self.target.write_text("value = 30\n")

        with self.assertRaises(PatchApprovalError):
            self.manager.save_proposal(self.task_id, patches)

    def test_duplicate_paths_rejected(self):
        patches = self.make_patch()
        patches.append(dict(patches[0]))

        with self.assertRaises(PatchApprovalError):
            self.manager.save_proposal(self.task_id, patches)

    def test_only_approved_proposal_can_enter_applying(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )

        with self.assertRaises(PatchApprovalError):
            self.manager.mark_applying(record["proposal_id"])

        self.manager.approve(record["proposal_id"])
        self.manager.mark_applying(record["proposal_id"])
        self.manager.mark_applied(record["proposal_id"])

        with self.assertRaises(PatchApprovalError):
            self.manager.mark_applying(record["proposal_id"])

    def test_apply_requires_approval_and_updates_file(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        transaction = PatchTransaction(self.engine)

        with self.assertRaises(PatchApprovalError):
            self.manager.apply_approved(record["proposal_id"], transaction)

        self.manager.approve(record["proposal_id"])
        with patch.object(
            self.engine,
            "run_registered_tests",
            return_value={"success": True, "returncode": 0},
        ):
            result = self.manager.apply_approved(
                record["proposal_id"], transaction
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(result["patches"]), 1)
        self.assertEqual(self.target.read_text(), "value = 20\n")
        self.assertEqual(
            self.manager.get_proposal(record["proposal_id"])["status"],
            "applied",
        )

    def test_failed_tests_roll_back_patch(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.manager.approve(record["proposal_id"])

        transaction = PatchTransaction(self.engine)

        with patch.object(
            self.engine,
            "run_registered_tests",
            return_value={"success": False, "returncode": 1},
        ):
            result = self.manager.apply_approved(
                record["proposal_id"], transaction
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rollback"], "complete")
        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertEqual(
            self.manager.get_proposal(record["proposal_id"])["status"],
            "failed",
        )

    def test_passing_tests_keep_patch(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.manager.approve(record["proposal_id"])

        transaction = PatchTransaction(self.engine)

        with patch.object(
            self.engine,
            "run_registered_tests",
            return_value={"success": True, "returncode": 0},
        ):
            result = self.manager.apply_approved(
                record["proposal_id"], transaction
            )

        self.assertTrue(result["success"])
        self.assertEqual(self.target.read_text(), "value = 20\n")
        self.assertEqual(
            self.manager.get_proposal(record["proposal_id"])["status"],
            "applied",
        )

    def test_apply_rejects_source_changed_after_approval(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.manager.approve(record["proposal_id"])
        self.target.write_text("value = 30\\n")

        transaction = PatchTransaction(self.engine)

        with self.assertRaises(PatchApprovalError):
            self.manager.apply_approved(record["proposal_id"], transaction)

        self.assertEqual(self.target.read_text(), "value = 30\\n")
        self.assertEqual(
            self.manager.get_proposal(record["proposal_id"])["status"],
            "approved",
        )

    def test_list_proposals(self):
        self.manager.save_proposal(self.task_id, self.make_patch())
        self.manager.save_proposal(self.task_id, self.make_patch())
        self.assertEqual(len(self.manager.list_proposals()), 2)

    def test_invalid_test_suite_rejected(self):
        with self.assertRaises(PatchApprovalError):
            self.manager.save_proposal(
                self.task_id, self.make_patch(), test_name="unregistered"
            )


    def test_concurrent_apply_claim_allows_only_one_winner(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        proposal_id = record["proposal_id"]
        self.manager.approve(proposal_id)

        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            try:
                self.manager.mark_applying(proposal_id)
                return "success"
            except PatchApprovalError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(results.count("success"), 1)
        self.assertEqual(results.count("rejected"), 1)
        self.assertEqual(
            self.manager.get_proposal(proposal_id)["status"],
            "applying",
        )


    def test_preview_diff_is_read_only(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch(), "Preview test"
        )

        before = self.target.read_bytes()
        previews = self.manager.preview_diff(record["proposal_id"])

        self.assertEqual(len(previews), 1)
        diff = "".join(previews[0]["diff"])
        self.assertIn("-value = 10", diff)
        self.assertIn("+value = 20", diff)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(
            self.manager.get_proposal(record["proposal_id"])["status"],
            "pending",
        )

    def test_preview_rejects_stale_source(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch(), "Stale preview test"
        )
        self.target.write_text("value = 30\\n", encoding="utf-8")

        with self.assertRaises(PatchApprovalError):
            self.manager.preview_diff(record["proposal_id"])

        self.assertEqual(
            self.manager.get_proposal(record["proposal_id"])["status"],
            "pending",
        )


    def test_save_proposal_rejected_at_maximum_attempts(self):
        task = dict(self.engine.get_task(self.task_id))
        task["repair_attempts"] = self.engine.policy.MAX_REPAIRS

        with patch.object(self.engine, "get_task", return_value=task):
            with self.assertRaisesRegex(PatchApprovalError, "Maximum repair attempts"):
                self.manager.save_proposal(
                    self.task_id, self.make_patch()
                )

        self.assertEqual(self.target.read_text(), "value = 10\n")

    def test_apply_rejected_at_maximum_attempts(self):
        record = self.manager.save_proposal(
            self.task_id, self.make_patch()
        )
        self.manager.approve(record["proposal_id"])

        task = dict(self.engine.get_task(self.task_id))
        task["repair_attempts"] = self.engine.policy.MAX_REPAIRS

        with patch.object(self.engine, "get_task", return_value=task):
            with self.assertRaisesRegex(PatchApprovalError, "Maximum repair attempts"):
                self.manager.apply_approved(
                    record["proposal_id"], PatchTransaction(self.engine)
                )

        self.assertEqual(self.target.read_text(), "value = 10\n")


if __name__ == "__main__":
    unittest.main()
