from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from agent_keepalive.providers.base import Snapshot
from agent_keepalive.providers.codex_recovery import CodexAppServerUnavailable
from agent_keepalive.runner import CODEX_APP_SERVER_RETRY_MAX_DELAY
from agent_keepalive.runner import Keeper


class KeeperAttachRetryTests(unittest.TestCase):
    def _keeper(self, attach_side_effect) -> Keeper:
        keeper = Keeper.__new__(Keeper)
        keeper.session = mock.Mock()
        keeper.session.attach.side_effect = attach_side_effect
        keeper.stop_requested = False
        keeper.record = mock.Mock()
        keeper.logger = mock.Mock()
        keeper._persist_state = mock.Mock()
        keeper._wait_for_retry = mock.Mock()
        return keeper

    def test_unavailable_codex_server_retries_and_recovers(self) -> None:
        unavailable = CodexAppServerUnavailable(
            Path("/tmp/codex.sock"),
            ConnectionRefusedError(111, "Connection refused"),
        )
        snapshot = Snapshot(
            target_id="thread-1",
            display_name="Example",
            status="idle",
            loaded=True,
            blocked=False,
            terminal=False,
            last_activity_at=None,
            last_event_at=None,
            idle_since=None,
            event_count=0,
            metadata={},
        )
        keeper = self._keeper([unavailable, unavailable, snapshot])

        with mock.patch("builtins.print"):
            result = keeper._attach_with_retry()

        self.assertIs(result, snapshot)
        self.assertEqual(keeper.session.attach.call_count, 3)
        self.assertEqual(
            keeper._wait_for_retry.call_args_list,
            [mock.call(5.0), mock.call(10.0)],
        )
        self.assertEqual(keeper.record.keeper_status, "waiting_for_codex_app_server")
        self.assertIsNone(keeper.record.last_error)
        self.assertEqual(keeper._persist_state.call_count, 2)
        warning_messages = [call.args[0] for call in keeper.logger.warning.call_args_list]
        self.assertIn("Codex app-server unavailable at /tmp/codex.sock", warning_messages[0])
        self.assertIn("retrying Codex app-server connection in 5s", warning_messages[0])

    def test_unavailable_backoff_is_capped(self) -> None:
        unavailable = CodexAppServerUnavailable(
            Path("/tmp/codex.sock"),
            "[Errno 111] Connection refused",
        )
        snapshot = object()
        keeper = self._keeper([unavailable] * 8 + [snapshot])

        with mock.patch("builtins.print"):
            self.assertIs(keeper._attach_with_retry(), snapshot)

        delays = [call.args[0] for call in keeper._wait_for_retry.call_args_list]
        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0])
        self.assertEqual(delays[-1], CODEX_APP_SERVER_RETRY_MAX_DELAY)

    def test_non_unavailable_attach_failure_is_not_masked(self) -> None:
        keeper = self._keeper([RuntimeError("protocol failure")])

        with self.assertRaisesRegex(RuntimeError, "protocol failure"):
            keeper._attach_with_retry()

        keeper._wait_for_retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
