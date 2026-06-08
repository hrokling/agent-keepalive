from __future__ import annotations

from datetime import timedelta
import unittest

from agent_keepalive.activity import ThreadActivityTracker
from agent_keepalive.timeparse import utc_now


class ThreadActivityTrackerTests(unittest.TestCase):
    def test_snapshot_uses_thread_updated_at_for_idle_threads(self) -> None:
        tracker = ThreadActivityTracker("thread-1")
        tracker.note_thread_snapshot(
            {
                "id": "thread-1",
                "name": "Example",
                "updatedAt": 1_700_000_000,
                "status": {"type": "idle"},
            }
        )
        self.assertEqual(tracker.status, "idle")
        self.assertIsNotNone(tracker.last_activity_at)
        self.assertEqual(tracker.idle_since, tracker.last_activity_at)

    def test_active_to_idle_resets_idle_since(self) -> None:
        tracker = ThreadActivityTracker("thread-1")
        now = utc_now()
        tracker.note_notification(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "thread-1", "status": {"type": "active"}},
            },
            observed_at=now,
        )
        later = now + timedelta(seconds=15)
        tracker.note_notification(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "thread-1", "status": {"type": "idle"}},
            },
            observed_at=later,
        )
        self.assertEqual(tracker.status, "idle")
        self.assertEqual(tracker.idle_since, later)
        self.assertEqual(tracker.last_activity_at, later)

    def test_should_not_detach_while_active(self) -> None:
        tracker = ThreadActivityTracker("thread-1")
        now = utc_now()
        tracker.note_notification(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "thread-1", "status": {"type": "active"}},
            },
            observed_at=now,
        )
        self.assertFalse(
            tracker.should_detach(
                60,
                now=now + timedelta(hours=4),
            )
        )


if __name__ == "__main__":
    unittest.main()
