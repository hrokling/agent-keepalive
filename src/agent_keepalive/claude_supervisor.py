from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .paths import AppPaths
from .providers.base import Snapshot
from .providers.base import should_detach
from .providers.claude import ClaudePreflight
from .providers.claude import list_live_entries
from .providers.claude import observe_claude_entry
from .providers.claude import preflight_claude
from .state import KeeperRecord
from .state import StateStore
from .state import process_is_alive
from .timeparse import parse_duration
from .timeparse import utc_now


POLL_INTERVAL = 15.0
CHILD_FAILURE_RETRY_INITIAL_DELAY = 30.0
CHILD_FAILURE_RETRY_MAX_DELAY = 300.0


def configure_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"agent_keepalive.{log_path.stem}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing_handler in logger.handlers:
        logger.removeHandler(existing_handler)
        existing_handler.close()

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
        self.idle_timeout_seconds = int(parse_duration(idle_timeout).total_seconds())
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
                "suppressed_sessions": {},
                "child_failure_retries": {},
            },
        )
        self.record.keeper_status = "attached"
        self.record.target_status = "idle"
        self.record.loaded = True
        self._suppressed_sessions: dict[str, dict[str, str]] = {}
        self._child_failure_counts: dict[str, int] = {}
        self._child_retry_not_before: dict[str, float] = {}

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
                self._tick(preflight)
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

    def _tick(self, preflight: ClaudePreflight) -> None:
        live_entries = list_live_entries(preflight.claude_bin, cwd=self.cwd)
        live_short_ids = {
            str(entry.get("sessionId", ""))[:8]
            for entry in live_entries
            if len(str(entry.get("sessionId", ""))[:8]) == 8
        }
        self._remove_stale_claude_records(live_short_ids)
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
            cwd = Path(str(entry.get("cwd", self.cwd or os.getcwd()))).resolve()
            snapshot = observe_claude_entry(
                preflight=preflight,
                short_id=short_id,
                cwd=cwd,
                live_entry=entry,
            )
            skip_reason = self._supervision_skip_reason(snapshot)
            if skip_reason is not None:
                self._suppress_session(short_id, snapshot.status, skip_reason)
                self._discard_dead_record(short_id)
                continue

            self._clear_suppression(short_id, snapshot.status)
            existing = self.store.load("claude", short_id)
            if existing and process_is_alive(existing.pid):
                self._clear_child_failure_retry(short_id)
                continue
            if existing and existing.keeper_status == "error":
                if not self._ready_to_retry_failed_child(short_id, existing.last_error):
                    continue
            elif existing is None and short_id in self._child_failure_counts:
                if not self._ready_to_retry_failed_child(short_id, "could not start keeper"):
                    continue
            if existing:
                self.store.remove("claude", short_id)
            try:
                self._spawn_keeper(short_id, cwd)
            except OSError as exc:
                self._schedule_child_failure_retry(short_id, f"could not spawn keeper: {exc}")

        self._persist_state()

    def _supervision_skip_reason(self, snapshot: Snapshot) -> str | None:
        if snapshot.terminal:
            return f"terminal Claude state {snapshot.status!r}"
        if not snapshot.metadata.get("state_available"):
            return "Claude job state metadata is unavailable"
        if snapshot.blocked:
            return "Claude session is blocked and waiting for its prerequisite"
        if should_detach(snapshot, self.idle_timeout_seconds, now=utc_now()):
            return (
                f"Claude session is non-active and already exceeds the "
                f"{self.idle_timeout_seconds}s idle timeout"
            )
        return None

    def _suppress_session(self, short_id: str, status: str, reason: str) -> None:
        current = {"status": status, "reason": reason}
        if self._suppressed_sessions.get(short_id) == current:
            return
        self._suppressed_sessions[short_id] = current
        self._sync_lifecycle_metadata()
        if reason.startswith("terminal"):
            self.logger.info(
                "removing Claude session %s from supervision: %s",
                short_id,
                reason,
            )
        else:
            self.logger.info(
                "suppressing Claude keeper for session %s: %s; discovery will recheck it",
                short_id,
                reason,
            )

    def _clear_suppression(self, short_id: str, status: str) -> None:
        previous = self._suppressed_sessions.pop(short_id, None)
        if previous is None:
            return
        self._sync_lifecycle_metadata()
        self.logger.info(
            "Claude session %s became eligible again (status=%s; previous reason: %s)",
            short_id,
            status,
            previous["reason"],
        )

    def _discard_dead_record(self, short_id: str) -> None:
        existing = self.store.load("claude", short_id)
        if existing and not process_is_alive(existing.pid):
            self.store.remove("claude", short_id)

    def _remove_stale_claude_records(self, live_short_ids: set[str]) -> None:
        for record in self.store.list(provider="claude"):
            if record.target_id == "all" or record.target_id in live_short_ids:
                continue
            if not process_is_alive(record.pid):
                self.store.remove(record.provider, record.target_id)
                self.logger.info(
                    "removed stale Claude keeper state for session %s after it left discovery",
                    record.target_id,
                )

    def _ready_to_retry_failed_child(self, short_id: str, error: str | None) -> bool:
        now = time.monotonic()
        retry_not_before = self._child_retry_not_before.get(short_id)
        if retry_not_before is None:
            self._schedule_child_failure_retry(short_id, error or "keeper exited with an unknown error")
            return False
        if now < retry_not_before:
            return False
        self._child_retry_not_before.pop(short_id, None)
        self.logger.info("retrying Claude keeper for session %s after backoff", short_id)
        return True

    def _schedule_child_failure_retry(self, short_id: str, error: str) -> None:
        attempt = self._child_failure_counts.get(short_id, 0) + 1
        delay = min(
            CHILD_FAILURE_RETRY_INITIAL_DELAY * (2 ** (attempt - 1)),
            CHILD_FAILURE_RETRY_MAX_DELAY,
        )
        self._child_failure_counts[short_id] = attempt
        self._child_retry_not_before[short_id] = time.monotonic() + delay
        self._sync_lifecycle_metadata()
        self.logger.warning(
            "Claude keeper for session %s failed: %s; retrying in %gs (attempt %s)",
            short_id,
            error,
            delay,
            attempt,
        )

    def _clear_child_failure_retry(self, short_id: str) -> None:
        if short_id not in self._child_failure_counts:
            return
        attempts = self._child_failure_counts.pop(short_id)
        self._child_retry_not_before.pop(short_id, None)
        self._sync_lifecycle_metadata()
        self.logger.info(
            "Claude keeper for session %s recovered after %s failed attempt(s)",
            short_id,
            attempts,
        )

    def _sync_lifecycle_metadata(self) -> None:
        self.record.provider_metadata["suppressed_sessions"] = dict(self._suppressed_sessions)
        self.record.provider_metadata["child_failure_retries"] = {
            short_id: {"attempt": attempt}
            for short_id, attempt in self._child_failure_counts.items()
        }

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
