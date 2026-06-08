from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .paths import AppPaths
from .providers.claude import list_live_entries
from .providers.claude import preflight_claude
from .state import KeeperRecord
from .state import StateStore
from .state import process_is_alive
from .timeparse import utc_now


POLL_INTERVAL = 15.0


def configure_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"agent_keepalive.{log_path.stem}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


class ClaudeDiscoverySupervisor:
    def __init__(
        self,
        *,
        claude_bin: str,
        cwd: Path | None,
        idle_timeout: str,
        state_root: Path,
    ) -> None:
        self.claude_bin = claude_bin
        self.cwd = cwd
        self.idle_timeout = idle_timeout
        self.paths = AppPaths(state_root)
        self.paths.ensure()
        self.store = StateStore(self.paths)
        self.log_path = self.paths.keeper_log_path("claude", "all")
        self.logger = configure_logger(self.log_path)
        self.stop_requested = False
        self.record = KeeperRecord.new(
            provider="claude",
            target_id="all",
            pid=os.getpid(),
            display_name="all Claude sessions",
            idle_timeout_seconds=0,
            log_path=self.log_path,
            selected_via="all",
            provider_metadata={
                "mode": "all",
                "claude_bin": claude_bin,
                "cwd": str(cwd) if cwd is not None else "",
            },
        )
        self.record.keeper_status = "attached"
        self.record.target_status = "idle"
        self.record.loaded = True

    def run(self) -> int:
        self._install_signal_handlers()
        self._persist_state()
        try:
            preflight = preflight_claude(self.claude_bin)
            self.record.provider_metadata.update(
                {
                    "claude_bin": preflight.claude_bin,
                    "claude_version": preflight.version,
                    "auth_method": preflight.auth_method,
                    "auth_error": preflight.auth_error,
                    "config_dir": str(preflight.config_dir),
                    "cwd": str(self.cwd) if self.cwd is not None else "",
                    "mode": "all",
                }
            )
            self.logger.info("starting Claude discovery supervisor cwd=%s", self.cwd or "<all>")
            while not self.stop_requested:
                self._tick(preflight.claude_bin)
                time.sleep(POLL_INTERVAL)
            self.record.stop_reason = "signal"
            self.record.keeper_status = "stopping"
            self._persist_state()
            return 0
        except BaseException as exc:  # noqa: BLE001
            self.record.keeper_status = "error"
            self.record.last_error = str(exc)
            self._persist_state()
            self.logger.exception("Claude discovery supervisor failed")
            print(f"agent-keepalive error: {exc}", file=sys.stderr)
            return 1
        finally:
            if self.record.keeper_status != "error":
                self.store.remove("claude", "all")

    def _tick(self, claude_bin: str) -> None:
        remove_stale_records(self.store)
        live_entries = list_live_entries(claude_bin, cwd=self.cwd)
        self.record.target_status = "active" if live_entries else "idle"
        self.record.event_count = len(live_entries)
        self.record.last_event_at = utc_now().isoformat()
        self.record.last_activity_at = self.record.last_event_at
        self.record.provider_metadata["live_sessions"] = len(live_entries)
        self._persist_state()

        for entry in live_entries:
            session_id = str(entry.get("sessionId", ""))
            short_id = session_id[:8] if session_id else ""
            if len(short_id) != 8:
                continue
            existing = self.store.load("claude", short_id)
            if existing and process_is_alive(existing.pid):
                continue
            if existing and not process_is_alive(existing.pid):
                self.store.remove("claude", short_id)
            self._spawn_keeper(short_id, Path(str(entry.get("cwd", self.cwd or os.getcwd()))).resolve())

    def _spawn_keeper(self, short_id: str, cwd: Path) -> None:
        self.logger.info("spawning Claude keeper for session %s cwd=%s", short_id, cwd)
        child_env = child_environment()
        child_log_path = self.paths.keeper_log_path("claude", short_id)
        child_log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "agent_keepalive",
            "run",
            "claude",
            "--session",
            short_id,
            "--claude-bin",
            self.claude_bin,
            "--idle-timeout",
            self.idle_timeout,
        ]
        if cwd:
            command.extend(["--cwd", str(cwd)])
        command.extend(["--state-root", str(self.paths.state_root), "--selected-via", "all"])
        with child_log_path.open("ab") as log_handle:
            subprocess.Popen(  # noqa: S603
                command,
                cwd=os.getcwd(),
                env=child_env,
                start_new_session=True,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )

    def _persist_state(self) -> None:
        self.store.save(self.record)

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame) -> None:
            self.stop_requested = True
            self.logger.info("received signal %s; stopping Claude discovery supervisor", signum)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


def child_environment() -> dict[str, str]:
    python_path_entries = [str(Path(__file__).resolve().parents[1])]
    if os.environ.get("PYTHONPATH"):
        python_path_entries.append(os.environ["PYTHONPATH"])
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(python_path_entries)
    return child_env


def remove_stale_records(store: StateStore) -> None:
    for record in store.list():
        if not process_is_alive(record.pid):
            store.remove(record.provider, record.target_id)
