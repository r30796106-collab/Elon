import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from engine.task_engine import TaskEngine
from engine.patch_proposal_service import PatchProposalService


class PatchProposalServiceTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        self.engine = TaskEngine(self.root)
        self.target = self.engine.workspace / "sample.py"
        self.target.write_text("value = 10\n", encoding="utf-8")

        self.task_id = self.engine.create_task("Proposal service test")

        self.provider = Mock()
        self.provider.configured.return_value = True

        self.service = PatchProposalService(
            self.engine,
            provider=self.provider,
        )

        self.service.repairer._collect_context = Mock(
            return_value={"sample.py": "value = 10\n"}
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_passing_baseline_does_not_generate_proposal(self):
        self.engine.run_registered_tests = Mock(
            return_value={"success": True, "returncode": 0}
        )

        result = self.service.generate_repair(self.task_id)

        self.assertTrue(result["success"])
        self.assertIsNone(result["proposal"])
        self.provider.propose_patch.assert_not_called()
        self.assertEqual(self.target.read_text(), "value = 10\n")

    def test_failed_baseline_saves_proposal_without_modifying_source(self):
        self.engine.run_registered_tests = Mock(
            return_value={
                "success": False,
                "returncode": 1,
                "stderr": "Simulated test failure",
            }
        )

        self.provider.propose_patch.return_value = {
            "action": "patch",
            "summary": "Update sample value",
            "path": "sample.py",
            "content": "value = 20\n",
        }

        result = self.service.generate_repair(self.task_id)

        self.assertTrue(result["success"])

        proposal = result["proposal"]
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(len(proposal["patches"]), 1)

        expected_hash = hashlib.sha256(
            b"value = 10\n"
        ).hexdigest()

        self.assertEqual(
            proposal["patches"][0]["expected_sha256"],
            expected_hash,
        )

        self.assertEqual(self.target.read_text(), "value = 10\n")
        self.assertEqual(
            self.engine.get_task(self.task_id)["repair_attempts"],
            0,
        )

        self.provider.propose_patch.assert_called_once()

    def test_missing_gemini_configuration_stops_safely(self):
        self.provider.configured.return_value = False

        with self.assertRaisesRegex(RuntimeError, "not configured"):
            self.service.generate_repair(self.task_id)

        self.engine.run_registered_tests = Mock()
        self.engine.run_registered_tests.assert_not_called()
        self.provider.propose_patch.assert_not_called()
        self.assertEqual(self.target.read_text(), "value = 10\n")


    def test_repair_generation_stops_at_maximum_attempts(self):
        task = dict(self.engine.get_task(self.task_id))
        task["repair_attempts"] = self.engine.policy.MAX_REPAIRS
        self.engine.get_task = Mock(return_value=task)

        with self.assertRaisesRegex(ValueError, "Maximum repair attempts"):
            self.service.generate_repair(self.task_id)

        self.engine.run_registered_tests = Mock()
        self.engine.run_registered_tests.assert_not_called()
        self.provider.propose_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
