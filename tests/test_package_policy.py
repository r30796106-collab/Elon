import tempfile
import unittest
from pathlib import Path

from security.policy import (
    SecurityPolicy,
    ApprovalRequired,
    PolicyError,
)


class PackagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.policy = SecurityPolicy(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()


    def test_autonomous_controller_is_protected(self):
        self.policy.home_root = self.policy.root
        target = self.policy.root / "engine" / "autonomous_controller.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# protected\\n", encoding="utf-8")

        with self.assertRaises(PolicyError):
            self.policy.validate_modifiable_file(
                "engine/autonomous_controller.py"
            )

    def test_all_recognized_managers_require_approval(self):
        for manager in self.policy.PACKAGE_MANAGERS:
            with self.subTest(manager=manager):
                with self.assertRaises(ApprovalRequired):
                    self.policy.validate_package_manager(manager)

    def test_unknown_manager_is_rejected(self):
        with self.assertRaises(PolicyError):
            self.policy.validate_package_manager("unknown-manager")

    def test_invalid_manager_type_is_rejected(self):
        for manager in (None, 123, [], {}):
            with self.subTest(manager=manager):
                with self.assertRaises(PolicyError):
                    self.policy.validate_package_manager(manager)


if __name__ == "__main__":
    unittest.main()
