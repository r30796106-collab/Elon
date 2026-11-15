import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.git_manager import GitManager, GitOperationError


class GitManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._git("init", "-b", "main")
        self._git("config", "user.name", "ELON Test")
        self._git("config", "user.email", "elon-test@example.invalid")
        (self.root / "sample.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        self._git("add", "sample.py")
        self._git("commit", "-m", "Initial test commit")
        self.manager = GitManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

    def test_status_reports_clean_repository(self):
        report = self.manager.status()

        self.assertTrue(report["success"])
        self.assertTrue(report["read_only"])
        self.assertEqual(report["branch"], "main")
        self.assertTrue(report["clean"])
        self.assertEqual(report["change_count"], 0)

    def test_status_reports_modified_and_untracked_files(self):
        (self.root / "sample.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
        )
        (self.root / "new.py").write_text(
            "VALUE = 3\n",
            encoding="utf-8",
        )

        report = self.manager.status()

        self.assertFalse(report["clean"])
        self.assertEqual(report["change_count"], 2)
        paths = {item["path"] for item in report["changes"]}
        self.assertEqual(paths, {"sample.py", "new.py"})

    def test_diff_stat_is_read_only(self):
        (self.root / "sample.py").write_text(
            "VALUE = 42\n",
            encoding="utf-8",
        )

        report = self.manager.diff(stat_only=True)

        self.assertTrue(report["read_only"])
        self.assertIn("sample.py", report["output"])
        self.assertEqual(
            (self.root / "sample.py").read_text(encoding="utf-8"),
            "VALUE = 42\n",
        )

    def test_log_returns_bounded_commit_history(self):
        report = self.manager.log(limit=1)

        self.assertTrue(report["success"])
        self.assertEqual(report["limit"], 1)
        self.assertEqual(len(report["commits"]), 1)
        self.assertIn("Initial test commit", report["commits"][0])

    def test_log_rejects_invalid_limits(self):
        for value in (0, 51, True, "10"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.manager.log(value)

    def test_non_repository_root_is_rejected(self):
        other = Path(self.temp.name) / "not-a-repo"
        other.mkdir()
        with self.assertRaises(GitOperationError):
            GitManager(other).status()

    def test_missing_git_is_reported(self):
        with patch("engine.git_manager.shutil.which", return_value=None):
            with self.assertRaisesRegex(GitOperationError, "unavailable"):
                self.manager.status()


if __name__ == "__main__":
    unittest.main()
