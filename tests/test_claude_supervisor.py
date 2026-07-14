from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_keepalive.claude_supervisor import CHILD_FAILURE_RETRY_INITIAL_DELAY
from agent_keepalive.claude_supervisor import ClaudeDiscoverySupervisor
from agent_keepalive.providers.base import Snapshot
from agent_keepalive.providers.base import should_detach
from agent_keepalive.providers.claude import ClaudePreflight
from agent_keepalive.state import KeeperRecord


class ClaudeDiscoverySupervisorTests(unittest.TestCase):
    session_id = "12345678-1234-1234-1234-123456789abc"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.supervisor = ClaudeDiscoverySupervisor(
            claude_bin="claude",
            cwd=Path("/repo"),
            idle_timeout="1h",
            state_root=Path(self.temp_dir.name),
        )
        self.preflight = ClaudePreflight(
            claude_bin="claude",
            version="2.1.168",
            auth_method="Claude Max account",
            auth_error=None,
            config_dir=Path(self.temp_dir.name) / "claude",
        )
        self.entry = {"sessionId": self.session_id, "cwd": "/repo"}

    def snapshot(
        self,
        *,
        status: str,
        blocked: bool = False,
        state_available: bool = True,
        last_activity_at: datetime | None = None,
    ) -> Snapshot:
        activity = last_activity_at or datetime.now(UTC)
        return Snapshot(
            target_id="12345678",
            display_name="Example Claude session",
            status=status,
            loaded=True,
            blocked=blocked,
            terminal=status in {"done", "failed", "error", "stopped", "cancelled"},
            last_activity_at=activity,
            last_event_at=activity,
            idle_since=None if status == "active" else activity,
            event_count=0,
            metadata={"seen": True, "state_available": state_available},
        )

    def tick(self, snapshot: Snapshot, *, monotonic: float = 0.0) -> mock.Mock:
        spawn = mock.Mock()
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch(
                    "agent_keepalive.claude_supervisor.list_live_entries",
                    return_value=[self.entry],
                )
            )
            stack.enter_context(
                mock.patch(
                    "agent_keepalive.claude_supervisor.observe_claude_entry",
                    return_value=snapshot,
                )
            )
            stack.enter_context(
                mock.patch("agent_keepalive.claude_supervisor.process_is_alive", return_value=False)
            )
            stack.enter_context(
                mock.patch("agent_keepalive.claude_supervisor.time.monotonic", return_value=monotonic)
            )
            stack.enter_context(mock.patch.object(self.supervisor, "_spawn_keeper", spawn))
            self.supervisor._tick(self.preflight)
        return spawn

    def test_old_blocked_session_reproduces_immediate_detach_condition_but_is_not_spawned(self) -> None:
        blocked = self.snapshot(
            status="blocked",
            blocked=True,
            last_activity_at=datetime.now(UTC) - timedelta(days=2),
        )
        self.assertTrue(should_detach(blocked, 3600, now=datetime.now(UTC)))

        first_spawn = self.tick(blocked)
        second_spawn = self.tick(blocked, monotonic=15.0)

        first_spawn.assert_not_called()
        second_spawn.assert_not_called()
        self.assertEqual(
            self.supervisor.record.provider_metadata["suppressed_sessions"]["12345678"]["reason"],
            "Claude session is blocked and waiting for its prerequisite",
        )

    def test_unavailable_state_metadata_is_suppressed_without_spawning(self) -> None:
        unavailable = self.snapshot(status="unknown", state_available=False)

        spawn = self.tick(unavailable)

        spawn.assert_not_called()
        self.assertEqual(
            self.supervisor.record.provider_metadata["suppressed_sessions"]["12345678"]["reason"],
            "Claude job state metadata is unavailable",
        )

    def test_blocked_session_becomes_eligible_and_recovers_when_active(self) -> None:
        blocked = self.snapshot(status="blocked", blocked=True)
        self.tick(blocked)

        spawn = self.tick(self.snapshot(status="active"), monotonic=15.0)

        spawn.assert_called_once_with("12345678", Path("/repo"))
        self.assertEqual(self.supervisor.record.provider_metadata["suppressed_sessions"], {})

    def test_healthy_active_session_is_supervised(self) -> None:
        spawn = self.tick(self.snapshot(status="active"))

        spawn.assert_called_once_with("12345678", Path("/repo"))
        self.assertEqual(self.supervisor.record.provider_metadata["suppressed_sessions"], {})

    def test_failed_child_is_reported_and_retried_with_backoff(self) -> None:
        failed = KeeperRecord.new(
            provider="claude",
            target_id="12345678",
            pid=999999,
            display_name="Example Claude session",
            idle_timeout_seconds=3600,
            log_path=Path(self.temp_dir.name) / "child.log",
            selected_via="all",
        )
        failed.keeper_status = "error"
        failed.last_error = "provider protocol failure"
        self.supervisor.store.save(failed)
        active = self.snapshot(status="active")

        initial = self.tick(active, monotonic=100.0)
        waiting = self.tick(active, monotonic=100.0 + CHILD_FAILURE_RETRY_INITIAL_DELAY - 1)
        retried = self.tick(active, monotonic=100.0 + CHILD_FAILURE_RETRY_INITIAL_DELAY)

        initial.assert_not_called()
        waiting.assert_not_called()
        retried.assert_called_once_with("12345678", Path("/repo"))
        self.assertEqual(self.supervisor.record.provider_metadata["child_failure_retries"]["12345678"]["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
