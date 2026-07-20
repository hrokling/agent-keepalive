from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from agent_keepalive.claude_supervisor import ClaudeDiscoverySupervisor
from agent_keepalive.claude_supervisor import session_target_id
from agent_keepalive.providers.claude import ClaudeDiscoveryResult


class ClaudeDiscoverySupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.root_a = self.base / "interactive"
        self.root_b = self.base / "opus"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.supervisor = self.make_supervisor([self.root_a, self.root_b])

    def make_supervisor(self, roots, **kwargs) -> ClaudeDiscoverySupervisor:
        return ClaudeDiscoverySupervisor(
            claude_bin="claude",
            config_roots=list(roots),
            cwd=None,
            idle_timeout="1h",
            state_root=self.base / "app-state",
            discovery_interval=20.0,
            disappearance_grace=60.0,
            terminal_grace=60.0,
            **kwargs,
        )

    def write_state(self, root: Path, short_id: str, **values) -> Path:
        state_dir = root / "jobs" / short_id
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": f"{short_id}-1234-1234-1234-123456789abc",
            "cwd": f"/repo/{root.name}",
            "state": "active",
            **values,
        }
        path = state_dir / "state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    def entry(short_id: str, root: Path, status: str = "running") -> dict[str, str]:
        return {
            "sessionId": f"{short_id}-1234-1234-1234-123456789abc",
            "cwd": f"/repo/{root.name}",
            "status": status,
        }

    def discovery(self, by_root):
        def discover(_bin, *, config_dir, cwd):
            del cwd
            value = by_root[config_dir]
            if isinstance(value, ClaudeDiscoveryResult):
                return value
            return ClaudeDiscoveryResult("success" if value else "empty", value)

        return mock.patch("agent_keepalive.claude_supervisor.discover_claude", side_effect=discover)

    def test_multiple_roots_and_identical_short_ids_do_not_collide(self) -> None:
        short_id = "12345678"
        self.write_state(self.root_a, short_id, name="Interactive")
        self.write_state(self.root_b, short_id, name="Opus")
        with self.discovery(
            {
                self.root_a: [self.entry(short_id, self.root_a)],
                self.root_b: [self.entry(short_id, self.root_b)],
            }
        ):
            self.supervisor.tick(monotonic_now=0)

        records = [r for r in self.supervisor.store.list(provider="claude") if r.target_id != "all"]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record.target_id for record in records}), 2)
        self.assertEqual(
            {record.provider_metadata["source_root"] for record in records},
            {str(self.root_a), str(self.root_b)},
        )
        self.assertTrue(all(record.pid == self.supervisor.record.pid for record in records))

    def test_roots_with_same_old_truncated_digest_remain_distinct(self) -> None:
        first = Path("/tmp/agent-keepalive-root-20338")
        second = Path("/tmp/agent-keepalive-root-27763")
        first_target = session_target_id(first, "12345678")
        second_target = session_target_id(second, "12345678")
        self.assertTrue(first_target.split("@", 1)[1].startswith("7cb9851f"))
        self.assertTrue(second_target.split("@", 1)[1].startswith("7cb9851f"))
        self.assertNotEqual(first_target, second_target)
        self.assertEqual(len(first_target.split("@", 1)[1]), 64)

    def test_all_mode_never_spawns_persistent_session_children(self) -> None:
        for index in range(5):
            short_id = f"{index + 1:08x}"
            self.write_state(self.root_a, short_id)
        entries = [self.entry(f"{index + 1:08x}", self.root_a) for index in range(5)]
        with self.discovery({self.root_a: entries, self.root_b: []}), mock.patch(
            "subprocess.Popen"
        ) as popen:
            self.supervisor.tick(monotonic_now=0)
        popen.assert_not_called()
        self.assertEqual(self.supervisor.record.provider_metadata["persistent_session_children"], 0)

    def test_discovery_invocations_scale_with_roots_and_due_polls_not_sessions(self) -> None:
        entries = []
        for index in range(20):
            short_id = f"{index + 1:08x}"
            self.write_state(self.root_a, short_id)
            entries.append(self.entry(short_id, self.root_a))
        with self.discovery({self.root_a: entries, self.root_b: []}) as discover:
            self.supervisor.tick(monotonic_now=0)
            self.supervisor.tick(monotonic_now=10)
            self.supervisor.tick(monotonic_now=20)
        self.assertEqual(discover.call_count, 4)  # two roots times two due polls

    def test_local_state_is_read_between_discovery_polls(self) -> None:
        path = self.write_state(self.root_a, "12345678", state="active")
        with self.discovery(
            {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        ) as discover:
            self.supervisor.tick(monotonic_now=0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["state"] = "blocked"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.supervisor.tick(monotonic_now=1)
        record = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "12345678")
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.target_status, "blocked")
        self.assertTrue(record.blocked)
        self.assertEqual(discover.call_count, 2)  # one call for each root, none at t=1

    def test_successful_empty_failed_and_invalid_discovery_are_distinct(self) -> None:
        results = {
            self.root_a: ClaudeDiscoveryResult("empty", []),
            self.root_b: ClaudeDiscoveryResult(
                "failure", [], "Claude discovery failed with exit code 7"
            ),
        }
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=0)
        metadata = self.supervisor.record.provider_metadata["discovery"]
        self.assertEqual(metadata[str(self.root_a)]["outcome"], "empty")
        self.assertEqual(metadata[str(self.root_b)]["outcome"], "failure")

        results[self.root_b] = ClaudeDiscoveryResult(
            "invalid_json", [], "Claude discovery returned invalid JSON"
        )
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=20)
        self.assertEqual(metadata[str(self.root_b)]["outcome"], "invalid_json")

    def test_missing_and_invalid_job_state_are_distinct(self) -> None:
        missing_id = "11111111"
        invalid_id = "22222222"
        invalid_path = self.root_a / "jobs" / invalid_id / "state.json"
        invalid_path.parent.mkdir(parents=True)
        invalid_path.write_text("not json", encoding="utf-8")
        entries = [self.entry(missing_id, self.root_a), self.entry(invalid_id, self.root_a)]
        with self.discovery({self.root_a: entries, self.root_b: []}):
            self.supervisor.tick(monotonic_now=0)
        missing = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, missing_id)
        )
        invalid = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, invalid_id)
        )
        self.assertEqual(missing.target_status, "state_missing")
        self.assertEqual(missing.provider_metadata["state_outcome"], "missing")
        self.assertEqual(invalid.target_status, "state_invalid")
        self.assertEqual(invalid.provider_metadata["state_outcome"], "invalid")

    def test_malformed_nested_job_state_isolated_to_one_session(self) -> None:
        bad_id = "11111111"
        good_id = "22222222"
        self.write_state(self.root_a, bad_id, state="idle", inFlight={"tasks": "bad"})
        self.write_state(self.root_a, good_id, state="active")
        entries = [self.entry(bad_id, self.root_a), self.entry(good_id, self.root_a)]
        with self.discovery({self.root_a: entries, self.root_b: []}):
            self.supervisor.tick(monotonic_now=0)
        invalid = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, bad_id)
        )
        active = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, good_id)
        )
        self.assertEqual(invalid.target_status, "state_invalid")
        self.assertEqual(active.target_status, "active")

    def test_malformed_state_and_discovery_cwds_never_abort_tick(self) -> None:
        state_id = "11111111"
        discovery_id = "22222222"
        self.write_state(self.root_a, state_id, state="active", cwd="bad\x00path")
        malformed_entry = self.entry(discovery_id, self.root_a)
        malformed_entry["cwd"] = "bad\x00path"
        entries = [self.entry(state_id, self.root_a), malformed_entry]
        with self.discovery({self.root_a: entries, self.root_b: []}):
            self.supervisor.tick(monotonic_now=0)
        invalid = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, state_id)
        )
        missing = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, discovery_id)
        )
        self.assertEqual(invalid.target_status, "state_invalid")
        self.assertEqual(missing.target_status, "state_missing")
        self.assertEqual(missing.provider_metadata["cwd"], str(self.root_a))

    def test_blocked_and_terminal_sessions_are_retained_for_visibility(self) -> None:
        self.write_state(self.root_a, "11111111", state="blocked")
        self.write_state(self.root_a, "22222222", state="done")
        entries = [self.entry("11111111", self.root_a), self.entry("22222222", self.root_a)]
        with self.discovery({self.root_a: entries, self.root_b: []}):
            self.supervisor.tick(monotonic_now=0)
        blocked = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "11111111")
        )
        terminal = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "22222222")
        )
        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.target_status, "blocked")
        self.assertTrue(terminal.terminal)
        self.assertEqual(terminal.target_status, "done")

    def test_terminal_record_expires_without_reappearing_from_unchanged_file(self) -> None:
        self.write_state(self.root_a, "22222222", state="done")
        entries = [self.entry("22222222", self.root_a)]
        results = {self.root_a: entries, self.root_b: []}
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=0)
            self.supervisor.tick(monotonic_now=60)
            self.assertIsNone(
                self.supervisor.store.load(
                    "claude", session_target_id(self.root_a, "22222222")
                )
            )
            self.supervisor.tick(monotonic_now=80)
        self.assertIsNone(
            self.supervisor.store.load("claude", session_target_id(self.root_a, "22222222"))
        )
        self.assertEqual(self.supervisor.record.provider_metadata["visible_sessions"], 0)

    def test_disappearance_is_marked_then_removed_after_grace(self) -> None:
        path = self.write_state(self.root_a, "12345678", updatedAt="2026-01-01T00:00:00Z")
        results = {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=0)
        path.unlink()
        results[self.root_a] = []
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=20)
            record = self.supervisor.store.load(
                "claude", session_target_id(self.root_a, "12345678")
            )
            self.assertEqual(record.target_status, "disappeared")
            self.assertEqual(record.last_activity_at, "2026-01-01T00:00:00+00:00")
            self.supervisor.tick(monotonic_now=79)
            self.assertIsNotNone(
                self.supervisor.store.load(
                    "claude", session_target_id(self.root_a, "12345678")
                )
            )
            self.supervisor.tick(monotonic_now=80)
        self.assertIsNone(
            self.supervisor.store.load("claude", session_target_id(self.root_a, "12345678"))
        )

    def test_failed_discovery_does_not_prove_disappearance(self) -> None:
        path = self.write_state(self.root_a, "12345678")
        results = {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=0)
        path.unlink()
        results[self.root_a] = ClaudeDiscoveryResult("failure", [], "exit code 2")
        with self.discovery(results):
            self.supervisor.tick(monotonic_now=20)
        record = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "12345678")
        )
        self.assertNotEqual(record.target_status, "disappeared")

    def test_local_monitoring_starts_even_when_discovery_fails(self) -> None:
        self.write_state(self.root_a, "12345678", state="blocked")
        failures = {
            self.root_a: ClaudeDiscoveryResult("failure", [], "exit code 2"),
            self.root_b: ClaudeDiscoveryResult("invalid_json", [], "invalid JSON"),
        }
        with self.discovery(failures):
            self.supervisor.tick(monotonic_now=0)
        record = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "12345678")
        )
        self.assertEqual(record.target_status, "blocked")
        self.assertTrue(record.blocked)

    def test_local_monitoring_survives_discovery_spawn_error(self) -> None:
        self.write_state(self.root_a, "12345678", state="active")
        with mock.patch(
            "agent_keepalive.providers.claude.run_cli", side_effect=OSError("SECRET")
        ):
            self.supervisor.tick(monotonic_now=0)
        record = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "12345678")
        )
        self.assertEqual(record.target_status, "active")
        discovery = self.supervisor.record.provider_metadata["discovery"][str(self.root_a)]
        self.assertEqual(discovery["outcome"], "failure")
        self.assertNotIn("SECRET", discovery["error"])

    def test_unknown_activity_remains_unknown(self) -> None:
        self.write_state(self.root_a, "12345678")
        with self.discovery(
            {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        ):
            self.supervisor.tick(monotonic_now=0)
        record = self.supervisor.store.load(
            "claude", session_target_id(self.root_a, "12345678")
        )
        self.assertIsNone(record.last_activity_at)

    def test_free_form_job_fields_are_not_persisted_or_logged(self) -> None:
        marker = "NEVER_PERSIST_CREDENTIAL_VALUE"
        self.write_state(
            self.root_a,
            "12345678",
            detail=marker,
            suggestedReply=marker,
            needs=marker,
            linkScanPath=marker,
        )
        with self.discovery(
            {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        ):
            self.supervisor.tick(monotonic_now=0)
        artifacts = "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.base / "app-state").rglob("*")
            if path.is_file()
        )
        self.assertNotIn(marker, artifacts)

    def test_unchanged_polls_do_not_log_transitions(self) -> None:
        self.write_state(self.root_a, "12345678", state="active")
        with self.discovery(
            {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        ):
            with mock.patch.object(self.supervisor.logger, "info") as info:
                self.supervisor.tick(monotonic_now=0)
                first_count = info.call_count
                self.supervisor.tick(monotonic_now=1)
                self.assertEqual(info.call_count, first_count)

    def test_shutdown_removes_owned_records_and_has_no_children(self) -> None:
        self.write_state(self.root_a, "12345678")
        with self.discovery(
            {self.root_a: [self.entry("12345678", self.root_a)], self.root_b: []}
        ), mock.patch("subprocess.Popen") as popen:
            self.supervisor.tick(monotonic_now=0)
            self.supervisor._remove_session_records()
        popen.assert_not_called()
        session_records = [
            r for r in self.supervisor.store.list(provider="claude") if r.target_id != "all"
        ]
        self.assertEqual(session_records, [])


if __name__ == "__main__":
    unittest.main()
