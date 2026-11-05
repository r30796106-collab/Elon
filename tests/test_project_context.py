import tempfile
import unittest
from pathlib import Path

from engine.project_context import ProjectContextBuilder


class TestProjectContextBuilder(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        (self.root / "engine").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "security").mkdir()

        (self.root / "engine" / "sample_engine.py").write_text(
            "def run_task():\n"
            "    return 'task execution'\n",
            encoding="utf-8",
        )

        (self.root / "tests" / "test_sample_engine.py").write_text(
            "def test_run_task():\n"
            "    assert True\n",
            encoding="utf-8",
        )

        (self.root / "security" / "credentials.py").write_text(
            "API_KEY = 'DO_NOT_INCLUDE'\n",
            encoding="utf-8",
        )

        self.builder = ProjectContextBuilder(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_builder_initializes(self):
        self.assertIsNotNone(self.builder)

    def test_context_build_returns_expected_structure(self):
        result = self.builder.build("run task execution")

        self.assertIsInstance(result, dict)
        self.assertIn("files", result)
        self.assertIn("summary", result)
        self.assertIsInstance(result["files"], list)

    def test_sensitive_security_file_is_not_in_context(self):
        result = self.builder.build("credentials API key security")

        rendered = str(result)
        self.assertNotIn("DO_NOT_INCLUDE", rendered)
        self.assertNotIn("security/credentials.py", rendered)

    def test_context_limits_are_defined(self):
        self.assertLessEqual(
            ProjectContextBuilder.MAX_CONTEXT_FILES, 12
        )
        self.assertLessEqual(
            ProjectContextBuilder.MAX_FILE_BYTES, 64 * 1024
        )
        self.assertLessEqual(
            ProjectContextBuilder.MAX_TOTAL_BYTES, 256 * 1024
        )

    def test_path_traversal_is_rejected(self):
        self.assertIsNone(
            self.builder._safe_path("../outside.py")
        )

    def test_hidden_path_is_rejected(self):
        hidden_dir = self.root / "engine" / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "secret.py").write_text(
            "SECRET = True\n", encoding="utf-8"
        )

        self.assertIsNone(
            self.builder._safe_path("engine/.hidden/secret.py")
        )

    def test_unapproved_root_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (other / "sample.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )

        self.assertIsNone(
            self.builder._safe_path("other/sample.py")
        )

    def test_security_directory_is_rejected(self):
        self.assertIsNone(
            self.builder._safe_path("security/credentials.py")
        )

    def test_oversized_file_is_rejected(self):
        large_file = self.root / "engine" / "large.py"
        large_file.write_bytes(
            b"x" * (ProjectContextBuilder.MAX_FILE_BYTES + 1)
        )

        self.assertIsNone(
            self.builder._safe_path("engine/large.py")
        )

    def test_symlink_parent_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "external.py").write_text(
            "EXTERNAL = True\n", encoding="utf-8"
        )

        link = self.root / "engine" / "linked_dir"

        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        self.assertIsNone(
            self.builder._safe_path("engine/linked_dir/external.py")
        )



    def test_relevant_test_is_selected_by_functionality(self):
        (self.root / "engine" / "calculator.py").write_text(
            "def calculate_total(price, quantity):\n"
            "    return price * quantity\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_calculator_validation.py").write_text(
            "def test_calculator_rejects_negative_quantity():\n"
            "    assert True\n",
            encoding="utf-8",
        )

        result = self.builder.build(
            "calculator validation negative quantity"
        )

        selected = {item["module"] for item in result["files"]}
        self.assertIn("engine.calculator", selected)
        self.assertIn("tests.test_calculator_validation", selected)
        self.assertIn(
            "tests.test_calculator_validation",
            result["tests"],
        )

    def test_unrelated_infrastructure_test_is_not_selected(self):
        (self.root / "tests" / "test_network_infrastructure.py").write_text(
            "def test_network_connection_pool():\n"
            "    assert True\n",
            encoding="utf-8",
        )

        result = self.builder.build(
            "calculator validation negative quantity"
        )

        selected = {item["module"] for item in result["files"]}
        self.assertNotIn(
            "tests.test_network_infrastructure",
            selected,
        )

    def test_dependency_modules_receive_relevance_boost(self):
        (self.root / "engine" / "calculator.py").write_text(
            "from engine.helpers import normalize_value\n"
            "def calculate(value):\n"
            "    return normalize_value(value)\n",
            encoding="utf-8",
        )
        (self.root / "engine" / "helpers.py").write_text(
            "def normalize_value(value):\n"
            "    return value\n",
            encoding="utf-8",
        )

        result = self.builder.build("calculator calculate")

        selected = {
            item["module"]: item for item in result["files"]
        }

        self.assertIn("engine.calculator", selected)
        self.assertIn("engine.helpers", selected)
        self.assertGreater(
            selected["engine.helpers"]["dependency_bonus"],
            0,
        )

if __name__ == "__main__":
    unittest.main()
