from __future__ import annotations

from datetime import timedelta
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_keepalive.cli import build_parser
from agent_keepalive.providers import get_provider
from agent_keepalive.providers.claude import observe_claude
from agent_keepalive.providers.claude import select_last_state
from agent_keepalive.providers.claude import short_session_id
from agent_keepalive.providers.claude import status_from_payload
from agent_keepalive.providers.codex import select_recent_thread
from agent_keepalive.providers.base import RunConfig
from agent_keepalive.state import KeeperRecord
from agent_keepalive.state import StateStore
from agent_keepalive.paths import AppPaths


class ProviderRegistryTests(unittest.TestCase):
    def test_dispatches_known_providers(self) -> None:
        self.assertEqual(get_provider("codex").name, "codex")
        self.assertEqual(get_provider("claude").name, "claude")

    def test_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            get_provider("missing")


class CliParserTests(unittest.TestCase):
    def test_new_codex_start_shape(self) -> None:
        args = build_parser().parse_args(["start", "codex", "--thread", "thread-1"])
        self.assertEqual(args.command, "start")
        self.assertEqual(args.provider, "codex")
        self.assertEqual(args.thread, "thread-1")

    def test_new_claude_run_shape(self) -> None:
        args = build_parser().parse_args(["run", "claude", "--session", "12345678", "--cwd", "/tmp"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.provider, "claude")
        self.assertEqual(args.session, "12345678")

    def test_new_claude_all_shape(self) -> None:
        args = build_parser().parse_args(["run", "claude", "--all"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.provider, "claude")
        self.assertTrue(args.all)


class StateTests(unittest.TestCase):
    def test_round_trips_generic_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(AppPaths(Path(temp_dir)))
            record = KeeperRecord.new(
                provider="claude",
                target_id="12345678",
                pid=123,
                display_name="Review",
                idle_timeout_seconds=3600,
                log_path=Path(temp_dir) / "log.txt",
                selected_via="session",
                provider_metadata={"cwd": "/repo"},
            )
            record.target_status = "blocked"
            record.blocked = True
            store.save(record)
            loaded = store.load("claude", "12345678")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.provider, "claude")
            self.assertEqual(loaded.target_id, "12345678")
            self.assertEqual(loaded.provider_metadata["cwd"], "/repo")
            self.assertTrue(loaded.blocked)


class CodexProviderTests(unittest.TestCase):
    def test_select_recent_prefers_loaded_then_updated(self) -> None:
        class Client:
            def list_loaded_threads(self):
                return ["older-loaded"]

            def list_threads(self, limit=20):
                return [
                    {"id": "newer", "updatedAt": 20, "status": {"type": "notLoaded"}},
                    {"id": "older-loaded", "updatedAt": 10, "status": {"type": "idle"}},
                ]

        self.assertEqual(select_recent_thread(Client())["id"], "older-loaded")


class ClaudeProviderTests(unittest.TestCase):
    def test_short_session_id_accepts_uuid_or_short(self) -> None:
        self.assertEqual(short_session_id("12345678"), "12345678")
        self.assertEqual(short_session_id("12345678-1234-1234-1234-123456789abc"), "12345678")
        with self.assertRaises(ValueError):
            short_session_id("too-long")

    def test_status_from_payload_classifies_states(self) -> None:
        self.assertEqual(status_from_payload({"state": "blocked"}, None), "blocked")
        self.assertEqual(status_from_payload({"state": "done"}, None), "done")
        self.assertEqual(status_from_payload({"state": "idle", "inFlight": {"tasks": 1}}, None), "active")
        self.assertEqual(status_from_payload({"state": "idle"}, {"status": "running"}), "active")

    def test_select_last_state_filters_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            first = config_dir / "jobs" / "11111111"
            second = config_dir / "jobs" / "22222222"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "state.json").write_text(
                '{"cwd": "/repo", "updatedAt": "2026-01-01T00:00:00Z"}',
                encoding="utf-8",
            )
            (second / "state.json").write_text(
                '{"cwd": "/repo", "updatedAt": "2026-01-02T00:00:00Z"}',
                encoding="utf-8",
            )
            state = select_last_state(config_dir, cwd=Path("/repo"))
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state["short_id"], "22222222")

    def test_observe_claude_reads_state_without_live_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            state_dir = config_dir / "jobs" / "12345678"
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                """
                {
                  "state": "blocked",
                  "detail": "waiting",
                  "sessionId": "12345678-1234-1234-1234-123456789abc",
                  "cwd": "/repo",
                  "updatedAt": "2026-01-01T00:00:00Z",
                  "suggestedReply": "continue"
                }
                """,
                encoding="utf-8",
            )
            with mock.patch("agent_keepalive.providers.claude.run_cli") as run_cli:
                run_cli.return_value.returncode = 0
                run_cli.return_value.stdout = "[]"
                run_cli.return_value.stderr = ""
                from agent_keepalive.providers.claude import ClaudePreflight

                snapshot = observe_claude(
                    preflight=ClaudePreflight(
                        claude_bin="claude",
                        version="2.1.168",
                        auth_method="Claude Max account",
                        auth_error=None,
                        config_dir=config_dir,
                    ),
                    short_id="12345678",
                    cwd=Path("/repo"),
                )
            self.assertEqual(snapshot.status, "blocked")
            self.assertTrue(snapshot.blocked)
            self.assertEqual(snapshot.metadata["suggested_reply"], "continue")


class SystemdTemplateTests(unittest.TestCase):
    def test_template_uses_agent_keepalive(self) -> None:
        content = Path("systemd/agent-keepalive@.service").read_text(encoding="utf-8")
        self.assertIn("agent-keepalive run", content)
        self.assertIn("Description=Agent keepalive for %i", content)
        self.assertIn("claude:*)", content)
        self.assertIn('target="%i"', content)


class RunConfigTests(unittest.TestCase):
    def test_run_config_holds_provider_metadata(self) -> None:
        config = RunConfig(
            provider="codex",
            target_id="thread-1",
            idle_timeout=timedelta(hours=1),
            state_root=Path("/tmp/state"),
            selected_via="thread",
            metadata={"socket_path": "/tmp/socket"},
        )
        self.assertEqual(config.metadata["socket_path"], "/tmp/socket")


if __name__ == "__main__":
    unittest.main()
