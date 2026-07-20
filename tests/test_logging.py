from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_keepalive.logging_utils import configure_logger


class BoundedLoggingTests(unittest.TestCase):
    def test_file_logging_rotates_to_a_bounded_number_of_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "agent_keepalive.logging_utils.LOG_MAX_BYTES", 256
        ), mock.patch("agent_keepalive.logging_utils.LOG_BACKUP_COUNT", 2), mock.patch.dict(
            "os.environ", {"AGENT_KEEPALIVE_LOG_DEST": "file"}
        ):
            path = Path(temp_dir) / "claude-all.log"
            logger = configure_logger("test.bounded", path)
            for index in range(100):
                logger.info("transition %s %s", index, "x" * 40)
            for handler in logger.handlers:
                handler.flush()
            files = list(Path(temp_dir).glob("claude-all.log*"))
            self.assertLessEqual(len(files), 3)
            self.assertLessEqual(sum(item.stat().st_size for item in files), 3 * 400)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_journal_logging_does_not_create_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            "os.environ", {"AGENT_KEEPALIVE_LOG_DEST": "journal"}
        ):
            path = Path(temp_dir) / "claude-all.log"
            logger = configure_logger("test.journal", path)
            logger.info("transition")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
