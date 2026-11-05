import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.self_repair import SelfRepairEngine
from engine.task_engine import TaskEngine
from providers.openai_provider import OpenAIProvider, ProviderError


class SelfRepairTests(unittest.TestCase):

    def test_missing_api_key_is_reported(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = OpenAIProvider()
            self.assertFalse(provider.configured())
            with self.assertRaises(ProviderError):
                provider.propose_patch({}, "core", {}, {})

    def test_legacy_single_file_repair_is_disabled(self):
        provider = Mock()
        engine = Mock()
        engine.get_task.return_value = {
            "id": 1,
            "status": "pending",
            "repair_attempts": 0,
        }

        repairer = SelfRepairEngine(Path.cwd(), provider=provider)

        with self.assertRaisesRegex(RuntimeError, "Direct repair is disabled"):
            repairer.repair_task(engine, 1)

        provider.propose_patch.assert_not_called()
        engine.apply_patch.assert_not_called()
        engine.run_registered_tests.assert_not_called()

    def test_no_repair_for_non_pending_task(self):
        provider = Mock()
        engine = Mock()
        engine.get_task.return_value = {
            "id": 1,
            "status": "completed",
            "repair_attempts": 0,
        }

        repairer = SelfRepairEngine(Path.cwd(), provider=provider)

        with self.assertRaises(ValueError):
            repairer.repair_task(engine, 1)

        provider.propose_patch.assert_not_called()
        engine.apply_patch.assert_not_called()

    def test_legacy_multi_file_repair_is_disabled_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = TaskEngine(root)

            a = engine.workspace / "a.py"
            b = engine.workspace / "b.py"
            a.write_text("value = 10\n")
            b.write_text("value = 20\n")

            task_id = engine.create_task("Multi-file safety test")
            provider = Mock()
            repairer = SelfRepairEngine(root, provider=provider)

            before_a = a.read_bytes()
            before_b = b.read_bytes()

            with self.assertRaisesRegex(
                RuntimeError, "Direct multi-file repair is disabled"
            ):
                repairer.repair_task_multi_file(engine, task_id)

            self.assertEqual(a.read_bytes(), before_a)
            self.assertEqual(b.read_bytes(), before_b)
            self.assertEqual(engine.get_task(task_id)["repair_attempts"], 0)
            provider.propose_patch_set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
