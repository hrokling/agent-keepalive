from __future__ import annotations

from datetime import timedelta
import argparse
from contextlib import redirect_stderr
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest import mock

from agent_keepalive.cli import build_parser
from agent_keepalive.cli import command_start
from agent_keepalive.providers import get_provider
from agent_keepalive.providers.claude import observe_claude
from agent_keepalive.providers.claude import discover_claude
from agent_keepalive.providers.claude import ClaudeProvider
from agent_keepalive.providers.claude import minimal_claude_environment
from agent_keepalive.providers.claude import preflight_claude
from agent_keepalive.providers.claude import read_job_state
from agent_keepalive.providers.claude import select_last_state
from agent_keepalive.providers.claude import short_session_id
from agent_keepalive.providers.claude import status_from_payload
from agent_keepalive.providers.codex import select_recent_thread
from agent_keepalive.providers.codex_recovery import CodexAppServerUnavailable
from agent_keepalive.providers.codex_recovery import CodexRecoveryError
from agent_keepalive.providers.codex_recovery import classify_version_state
from agent_keepalive.providers.codex_recovery import ensure_current_codex_app_server
from agent_keepalive.providers.codex_recovery import parse_daemon_version
from agent_keepalive.providers.codex_recovery import probe_socket_app_server_version
from agent_keepalive.providers.base import RunConfig
from agent_keepalive.providers.base import ResolvedTarget
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
        args = build_parser().parse_args(
            ["run", "claude", "--all", "--config-root", "/one", "--config-root", "/two"]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(args.provider, "claude")
        self.assertTrue(args.all)
        self.assertEqual(args.config_root, ["/one", "/two"])


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


class CodexRecoveryTests(unittest.TestCase):
    @mock.patch("agent_keepalive.providers.codex_recovery.AppServerClient")
    def test_probe_classifies_connection_refused_as_unavailable(self, app_server_client) -> None:
        app_server_client.return_value.connect.side_effect = ConnectionRefusedError(
            111,
            "Connection refused",
        )

        with self.assertRaises(CodexAppServerUnavailable) as context:
            probe_socket_app_server_version(Path("/tmp/codex.sock"))

        self.assertEqual(context.exception.socket_path, Path("/tmp/codex.sock"))
        self.assertIn("Codex app-server unavailable", str(context.exception))
        app_server_client.return_value.close.assert_called_once_with()

    def test_missing_socket_is_classified_before_daemon_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "missing.sock"
            with mock.patch("agent_keepalive.providers.codex_recovery.run_codex_command") as run_command:
                with self.assertRaises(CodexAppServerUnavailable):
                    ensure_current_codex_app_server(socket_path)
                run_command.assert_not_called()

    @mock.patch("agent_keepalive.providers.codex_recovery.AppServerClient")
    def test_probe_does_not_retry_permission_errors(self, app_server_client) -> None:
        app_server_client.return_value.connect.side_effect = PermissionError(
            13,
            "Permission denied",
        )

        with self.assertRaises(PermissionError):
            probe_socket_app_server_version(Path("/tmp/codex.sock"))

    def test_parse_daemon_version_reads_machine_output(self) -> None:
        daemon = parse_daemon_version(
            """
            {
              "status": "running",
              "socketPath": "/tmp/codex.sock",
              "cliVersion": "0.144.1",
              "managedCodexVersion": "0.144.1",
              "appServerVersion": "0.142.0"
            }
            """
        )
        self.assertEqual(daemon.status, "running")
        self.assertEqual(daemon.socket_path, Path("/tmp/codex.sock"))
        self.assertEqual(daemon.cli_version, "0.144.1")
        self.assertEqual(daemon.managed_codex_version, "0.144.1")
        self.assertEqual(daemon.app_server_version, "0.142.0")

    def test_classify_version_state_detects_stale_server(self) -> None:
        self.assertEqual(classify_version_state("0.144.1", "0.144.1"), "current")
        self.assertEqual(classify_version_state("0.142.0", "0.144.1"), "stale")
        with self.assertRaises(CodexRecoveryError):
            classify_version_state("0.145.0", "0.144.1")

    @mock.patch("agent_keepalive.providers.codex_recovery.probe_socket_app_server_version")
    @mock.patch("agent_keepalive.providers.codex_recovery.run_codex_command")
    def test_ensure_current_accepts_current_server(self, run_codex_command, probe_version) -> None:
        run_codex_command.return_value = subprocess.CompletedProcess(
            args=["codex", "app-server", "daemon", "version"],
            returncode=0,
            stdout=(
                '{"status":"running","socketPath":"/tmp/codex.sock","cliVersion":"0.144.1",'
                '"managedCodexVersion":"0.144.1","appServerVersion":"0.144.1"}'
            ),
            stderr="",
        )
        probe_version.return_value = "0.144.1"

        state = ensure_current_codex_app_server(Path("/tmp/codex.sock"))

        self.assertEqual(state.expected_version, "0.144.1")
        self.assertEqual(state.app_server_version, "0.144.1")
        self.assertEqual(state.recovery_action, "none")
        self.assertEqual(run_codex_command.call_count, 1)

    @mock.patch("agent_keepalive.providers.codex_recovery.probe_socket_app_server_version")
    @mock.patch("agent_keepalive.providers.codex_recovery.run_codex_command")
    def test_ensure_current_refuses_stale_custom_socket(self, run_codex_command, probe_version) -> None:
        run_codex_command.return_value = subprocess.CompletedProcess(
            args=["codex", "app-server", "daemon", "version"],
            returncode=0,
            stdout=(
                '{"status":"running","socketPath":"/tmp/daemon.sock","cliVersion":"0.144.1",'
                '"managedCodexVersion":"0.144.1","appServerVersion":"0.144.1"}'
            ),
            stderr="",
        )
        probe_version.return_value = "0.142.0"

        with self.assertRaises(CodexRecoveryError):
            ensure_current_codex_app_server(Path("/tmp/custom.sock"))

        self.assertEqual(run_codex_command.call_count, 1)

    @mock.patch("agent_keepalive.providers.codex_recovery.terminate_process")
    @mock.patch("agent_keepalive.providers.codex_recovery.find_codex_listener_pid")
    @mock.patch("agent_keepalive.providers.codex_recovery.probe_socket_app_server_version")
    @mock.patch("agent_keepalive.providers.codex_recovery.run_codex_command")
    def test_ensure_current_recovers_unmanaged_stale_server(
        self,
        run_codex_command,
        probe_version,
        find_listener_pid,
        terminate_process,
    ) -> None:
        daemon_json = (
            '{"status":"running","socketPath":"/tmp/codex.sock","cliVersion":"0.144.1",'
            '"managedCodexVersion":"0.144.1","appServerVersion":"0.142.0"}'
        )
        current_json = (
            '{"status":"running","socketPath":"/tmp/codex.sock","cliVersion":"0.144.1",'
            '"managedCodexVersion":"0.144.1","appServerVersion":"0.144.1"}'
        )
        run_codex_command.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "app-server", "daemon", "version"],
                returncode=0,
                stdout=daemon_json,
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "app-server", "daemon", "restart"],
                returncode=1,
                stdout="",
                stderr="app server is running but is not managed by codex app-server daemon",
            ),
            subprocess.CompletedProcess(
                args=["codex", "app-server", "daemon", "restart"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "app-server", "daemon", "version"],
                returncode=0,
                stdout=current_json,
                stderr="",
            ),
        ]
        probe_version.side_effect = ["0.142.0", "0.144.1"]
        find_listener_pid.return_value = 4242
        terminate_process.return_value = "sigterm"

        state = ensure_current_codex_app_server(Path("/tmp/codex.sock"))

        self.assertEqual(state.app_server_version, "0.144.1")
        self.assertEqual(state.recovery_action, "stop_unmanaged_listener_sigterm_then_daemon_restart")
        find_listener_pid.assert_called_once_with(Path("/tmp/codex.sock"))
        terminate_process.assert_called_once_with(4242)
        self.assertEqual(run_codex_command.call_count, 4)


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
            self.assertNotIn("suggested_reply", snapshot.metadata)

    def test_unknown_activity_is_not_replaced_with_current_time(self) -> None:
        from agent_keepalive.providers.claude import ClaudePreflight

        snapshot = observe_claude_entry_for_test(
            ClaudePreflight("claude", "2.1.215", None, None, Path("/tmp/root")),
            state={"state": "active"},
        )
        self.assertIsNone(snapshot.last_activity_at)

    def test_job_state_outcomes_distinguish_missing_invalid_and_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(read_job_state(root, "11111111").outcome, "missing")
            path = root / "jobs" / "11111111" / "state.json"
            path.parent.mkdir(parents=True)
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(read_job_state(root, "11111111").outcome, "invalid")
            path.write_text('{"state":"blocked"}', encoding="utf-8")
            self.assertEqual(read_job_state(root, "11111111").outcome, "ok")

    def test_malformed_consumed_job_fields_are_invalid_and_never_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "jobs" / "11111111" / "state.json"
            path.parent.mkdir(parents=True)
            for payload in (
                {"state": "idle", "updatedAt": "not-a-timestamp"},
                {"state": "idle", "inFlight": {"tasks": "not-a-number"}},
                {"state": "idle", "inFlight": []},
                {"state": "active", "cwd": "bad\x00path"},
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(read_job_state(root, "11111111").outcome, "invalid")
                self.assertIsInstance(status_from_payload(payload, None), str)
            path.write_bytes(b"\xff")
            self.assertEqual(read_job_state(root, "11111111").outcome, "invalid")

    def test_fake_claude_receives_minimal_credential_free_environment(self) -> None:
        credential_names = [
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "SPREADSTATION_CLAUDE_OAUTH_TOKEN_FILE",
            "AWS_SECRET_ACCESS_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ]
        marker = "NEVER_PERSIST_THIS_CREDENTIAL"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fake = root / "fake-claude"
            capture = root / "capture.json"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "names = " + repr(credential_names) + "\n"
                "if '--version' in sys.argv:\n"
                "    print('2.1.215 (Claude Code)')\n"
                "else:\n"
                "    with open(" + repr(str(capture)) + ", 'w') as f:\n"
                "        json.dump({'present': [n for n in names if n in os.environ], 'argv': sys.argv[1:]}, f)\n"
                "    print('[]')\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            polluted = {name: marker for name in credential_names}
            with mock.patch.dict(os.environ, polluted, clear=False):
                preflight = preflight_claude(str(fake), config_dir=root)
                result = discover_claude(preflight.claude_bin, config_dir=root, cwd=None)
            self.assertEqual(result.outcome, "empty")
            captured = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(captured["present"], [])
            self.assertNotIn("auth", captured["argv"])
            artifacts = "".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(marker, artifacts)

    def test_discovery_failure_and_invalid_json_do_not_expose_output(self) -> None:
        secret = "SECRET_OUTPUT_MARKER"
        failure = subprocess.CompletedProcess(["claude"], 9, secret, secret)
        invalid = subprocess.CompletedProcess(["claude"], 0, secret, "")
        with mock.patch("agent_keepalive.providers.claude.run_cli", return_value=failure):
            result = discover_claude("claude", config_dir=Path("/tmp/root"), cwd=None)
        self.assertEqual(result.outcome, "failure")
        self.assertNotIn(secret, result.error)
        with mock.patch("agent_keepalive.providers.claude.run_cli", return_value=invalid):
            result = discover_claude("claude", config_dir=Path("/tmp/root"), cwd=None)
        self.assertEqual(result.outcome, "invalid_json")
        self.assertNotIn(secret, result.error)

    def test_discovery_spawn_and_decode_errors_are_typed_failures(self) -> None:
        failures = (
            (OSError("SECRET_OS_ERROR"), "failure"),
            (UnicodeDecodeError("utf-8", b"x", 0, 1, "SECRET_DECODE_ERROR"), "invalid_json"),
        )
        for error, expected in failures:
            with mock.patch("agent_keepalive.providers.claude.run_cli", side_effect=error):
                result = discover_claude("claude", config_dir=Path("/tmp/root"), cwd=None)
            self.assertEqual(result.outcome, expected)
            self.assertNotIn("SECRET", result.error)

    def test_malformed_discovery_cwd_is_invalid_json(self) -> None:
        malformed = subprocess.CompletedProcess(
            ["claude"],
            0,
            '[{"sessionId":"12345678-1234-1234-1234-123456789abc","cwd":"bad\\u0000path"}]',
            "",
        )
        with mock.patch("agent_keepalive.providers.claude.run_cli", return_value=malformed):
            result = discover_claude("claude", config_dir=Path("/tmp/root"), cwd=None)
        self.assertEqual(result.outcome, "invalid_json")
        self.assertEqual(result.entries, [])

    def test_minimal_environment_is_an_allowlist(self) -> None:
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "marker"}, clear=False):
            environment = minimal_claude_environment(Path("/tmp/root"), claude_bin="claude")
        self.assertEqual(set(environment), {"HOME", "PATH", "LANG", "CLAUDE_CONFIG_DIR"})
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_supervisor_records_are_rendered_without_claude_subprocesses(self) -> None:
        record = KeeperRecord.new(
            provider="claude",
            target_id="12345678@abcdef12",
            pid=123,
            display_name="12345678",
            idle_timeout_seconds=0,
            log_path=Path("/tmp/log"),
            selected_via="all",
            provider_metadata={"managed_by_supervisor": True, "source_root": "/tmp/root"},
        )
        record.target_status = "blocked"
        record.blocked = True
        with mock.patch("agent_keepalive.providers.claude.preflight_claude") as preflight:
            view = ClaudeProvider().live_view([record])
        preflight.assert_not_called()
        self.assertEqual(view[record.target_id].status, "blocked")


class SystemdTemplateTests(unittest.TestCase):
    def test_template_uses_agent_keepalive(self) -> None:
        content = Path("systemd/agent-keepalive@.service").read_text(encoding="utf-8")
        self.assertIn("agent-keepalive service %i", content)
        self.assertIn("Description=Agent keepalive for %i", content)
        self.assertIn("AGENT_KEEPALIVE_CLAUDE_CONFIG_ROOTS", content)
        self.assertIn("UnsetEnvironment=CLAUDE_CODE_OAUTH_TOKEN", content)
        self.assertIn("SPREADSTATION_CLAUDE_OAUTH_TOKEN_FILE", content)
        self.assertIn("AWS_SECRET_ACCESS_KEY", content)
        self.assertIn("AGENT_KEEPALIVE_LOG_DEST=file", content)
        self.assertIn("KillMode=control-group", content)
        self.assertNotIn("/bin/sh -l", content)


class StartCommandTests(unittest.TestCase):
    def test_failed_claude_all_start_never_tails_legacy_log(self) -> None:
        marker = "LEGACY_SECRET_MARKER"
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            legacy = state_root / "logs" / "claude-all.log"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(marker, encoding="utf-8")
            args = argparse.Namespace(
                provider="claude",
                state_root=str(state_root),
                idle_timeout="1h",
            )
            provider = mock.Mock()
            provider.resolve.return_value = ResolvedTarget(
                provider="claude",
                target_id="all",
                display_name="Claude discovery supervisor",
                selected_via="all",
                metadata={
                    "claude_bin": "claude",
                    "cwd": "",
                    "config_roots": ["/tmp/root"],
                },
            )
            process = mock.Mock(pid=999999)
            process.poll.return_value = 1
            stderr = io.StringIO()
            with mock.patch("agent_keepalive.cli.get_provider", return_value=provider), mock.patch(
                "agent_keepalive.cli.subprocess.Popen", return_value=process
            ), redirect_stderr(stderr):
                result = command_start(args)
            self.assertEqual(result, 1)
            self.assertNotIn(marker, stderr.getvalue())


def observe_claude_entry_for_test(preflight, *, state):
    from agent_keepalive.providers.claude import snapshot_from_claude_sources

    return snapshot_from_claude_sources(
        preflight=preflight,
        short_id="12345678",
        cwd=Path("/repo"),
        state=state,
        live_entry=None,
    )


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
