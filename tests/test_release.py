from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

from agent_keepalive import __version__


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_version_is_consistent(self) -> None:
        metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, "0.3.1")
        self.assertEqual(metadata["project"]["version"], __version__)
        self.assertIn("## 0.3.1", Path("CHANGELOG.md").read_text(encoding="utf-8"))

    def test_cli_reports_release_version(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "agent_keepalive", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "agent-keepalive 0.3.1")


if __name__ == "__main__":
    unittest.main()
