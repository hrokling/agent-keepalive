from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

from .logging_utils import configure_logger
from .paths import AppPaths
from .providers.claude import ClaudeDiscoveryResult
from .providers.claude import JobStateResult
from .providers.claude import discover_claude
from .providers.claude import preflight_claude
from .providers.claude import read_job_state
from .providers.claude import snapshot_from_claude_sources
from .providers.claude import short_session_id
from .providers.claude import value_as_str
from .state import KeeperRecord
from .state import StateStore
from .timeparse import isoformat_or_none
from .timeparse import utc_now


DISCOVERY_INTERVAL = 20.0
LOCAL_STATE_INTERVAL = 1.0
DISAPPEARANCE_GRACE_SECONDS = 60.0
TERMINAL_GRACE_SECONDS = 60.0


@dataclass
class SessionLifecycle:
    target_id: str
    config_root: Path
    short_id: str
    missing_since: float | None = None
    terminal_since: float | None = None
    signature: tuple[object, ...] | None = None


def root_fingerprint(config_root: Path) -> str:
    return hashlib.sha256(str(config_root.resolve()).encode("utf-8")).hexdigest()


def session_target_id(config_root: Path, short_id: str) -> str:
    return f"{short_id}@{root_fingerprint(config_root)}"


class ClaudeDiscoverySupervisor:
    """One process that discovers and records sessions across Claude roots."""

    def __init__(
        self,
        *,
        claude_bin: str,
        config_roots: list[Path],
        cwd: Path | None,
        idle_timeout: str,
        state_root: Path,
        discovery_interval: float = DISCOVERY_INTERVAL,
        local_state_interval: float = LOCAL_STATE_INTERVAL,
        disappearance_grace: float = DISAPPEARANCE_GRACE_SECONDS,
        terminal_grace: float = TERMINAL_GRACE_SECONDS,
    ) -> None:
        del idle_timeout  # Session execution is independent; records do not time it out.
        if not config_roots:
            raise ValueError("at least one Claude config root is required")
        self.claude_bin = claude_bin
        self.config_roots = list(dict.fromkeys(root.expanduser().resolve() for root in config_roots))
        self.cwd = cwd
        self.discovery_interval = discovery_interval
        self.local_state_interval = local_state_interval
        self.disappearance_grace = disappearance_grace
        self.terminal_grace = terminal_grace
        self.paths = AppPaths(state_root)
        self.paths.ensure()
        self.store = StateStore(self.paths)
        for stale in self.store.list(provider="claude"):
            if stale.provider_metadata.get("managed_by_supervisor"):
                self.store.remove(stale.provider, stale.target_id)
        self.log_path = self.paths.keeper_log_path("supervisor", "claude")
        self.logger = configure_logger("agent_keepalive.claude_all", self.log_path)
        self.stop_requested = False
        self.record = KeeperRecord.new(
            provider="claude",
            target_id="all",
            pid=os.getpid(),
            display_name="Claude discovery supervisor",
            idle_timeout_seconds=0,
            log_path=self.log_path,
            selected_via="all",
            provider_metadata={
                "mode": "all",
                "claude_bin": claude_bin,
                "config_roots": [str(root) for root in self.config_roots],
                "discovery_interval_seconds": discovery_interval,
                "local_state_interval_seconds": local_state_interval,
                "disappearance_grace_seconds": disappearance_grace,
                "terminal_grace_seconds": terminal_grace,
                "persistent_session_children": 0,
                "discovery": {},
            },
        )
        self.record.target_status = "idle"
        self.record.loaded = True
        self._last_discovery_at: dict[Path, float] = {}
        self._discovery_entries: dict[Path, dict[str, dict[str, Any]]] = {
            root: {} for root in self.config_roots
        }
        self._discovery_results: dict[Path, ClaudeDiscoveryResult] = {}
        self._sessions: dict[tuple[Path, str], SessionLifecycle] = {}
        self._retired_terminal: set[tuple[Path, str]] = set()
        self._transition_count = 0

    def run(self) -> int:
        self._install_signal_handlers()
        self._persist_supervisor()
        try:
            preflight = preflight_claude(self.claude_bin, config_dir=self.config_roots[0])
            self.claude_bin = preflight.claude_bin
            self.record.provider_metadata.update(
                {"claude_bin": preflight.claude_bin, "claude_version": preflight.version}
            )
            self.record.keeper_status = "attached"
            self._persist_supervisor()
            self.logger.info(
                "starting single Claude discovery supervisor roots=%s cwd=%s",
                len(self.config_roots),
                self.cwd or "<all>",
            )
            while not self.stop_requested:
                self.tick()
                self._wait(self.local_state_interval)
            self.record.stop_reason = "signal"
            self.record.keeper_status = "stopping"
            self._persist_supervisor()
            return 0
        except BaseException as exc:  # noqa: BLE001
            self.record.keeper_status = "error"
            self.record.last_error = type(exc).__name__
            self._persist_supervisor()
            self.logger.error(
                "Claude discovery supervisor failed error_type=%s", type(exc).__name__
            )
            print(f"agent-keepalive error: {type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            self._remove_session_records()
            if self.record.keeper_status != "error":
                self.store.remove("claude", "all")

    def tick(self, *, monotonic_now: float | None = None) -> None:
        now_mono = time.monotonic() if monotonic_now is None else monotonic_now
        for config_root in self.config_roots:
            self._refresh_discovery_if_due(config_root, now_mono)
            self._refresh_root_records(config_root, now_mono)
        active = sum(
            1
            for lifecycle in self._sessions.values()
            if lifecycle.missing_since is None and lifecycle.terminal_since is None
        )
        self.record.target_status = "active" if active else "idle"
        self.record.event_count = self._transition_count
        self.record.provider_metadata["visible_sessions"] = len(self._sessions)
        self._persist_supervisor()

    def _refresh_discovery_if_due(self, config_root: Path, now_mono: float) -> None:
        last = self._last_discovery_at.get(config_root)
        if last is not None and now_mono - last < self.discovery_interval:
            return
        self._last_discovery_at[config_root] = now_mono
        result = discover_claude(
            self.claude_bin,
            config_dir=config_root,
            cwd=self.cwd,
        )
        previous = self._discovery_results.get(config_root)
        self._discovery_results[config_root] = result
        if result.outcome in {"success", "empty"}:
            entries: dict[str, dict[str, Any]] = {}
            for entry in result.entries:
                session_id = value_as_str(entry, "sessionId") or ""
                candidate = (
                    value_as_str(entry, "shortSessionId")
                    or value_as_str(entry, "daemonShort")
                    or session_id
                )
                try:
                    short_id = short_session_id(candidate)
                except ValueError:
                    continue
                entries[short_id] = entry
            self._discovery_entries[config_root] = entries
        self.record.provider_metadata["discovery"][str(config_root)] = {
            "outcome": result.outcome,
            "entry_count": len(result.entries),
            "error": result.error,
            "checked_at": utc_now().isoformat(),
        }
        if previous is None or (previous.outcome, previous.error) != (result.outcome, result.error):
            level = logging.INFO if result.outcome in {"success", "empty"} else logging.WARNING
            self.logger.log(
                level,
                "Claude discovery root=%s outcome=%s entries=%s%s",
                config_root,
                result.outcome,
                len(result.entries),
                f" error={result.error}" if result.error else "",
            )

    def _refresh_root_records(self, config_root: Path, now_mono: float) -> None:
        local_ids = self._local_job_ids(config_root)
        live_entries = self._discovery_entries[config_root]
        known_ids = {
            short_id for root, short_id in self._sessions if root == config_root
        }
        candidates = local_ids | set(live_entries) | known_ids
        discovery = self._discovery_results.get(config_root)
        discovery_authoritative = discovery is not None and discovery.outcome in {"success", "empty"}

        for short_id in sorted(candidates):
            key = (config_root, short_id)
            lifecycle = self._sessions.get(key)
            if lifecycle is None:
                lifecycle = SessionLifecycle(
                    target_id=session_target_id(config_root, short_id),
                    config_root=config_root,
                    short_id=short_id,
                )
                self._sessions[key] = lifecycle

            state_result = read_job_state(config_root, short_id)
            live_entry = live_entries.get(short_id)
            locally_present = state_result.outcome in {"ok", "invalid"}
            if not locally_present and live_entry is None and discovery_authoritative:
                if lifecycle.missing_since is None:
                    lifecycle.missing_since = now_mono
                    self._write_disappeared_record(lifecycle)
                elif now_mono - lifecycle.missing_since >= self.disappearance_grace:
                    self._remove_lifecycle(key, lifecycle, "disappearance grace expired")
                continue

            if live_entry is None and not locally_present and not discovery_authoritative:
                continue

            lifecycle.missing_since = None
            snapshot = snapshot_from_claude_sources(
                preflight=None,
                short_id=short_id,
                cwd=self._entry_cwd(config_root, live_entry, state_result),
                state=state_result.payload,
                live_entry=live_entry,
                state_outcome=state_result.outcome,
                config_dir=config_root,
                claude_bin=self.claude_bin,
            )
            if key in self._retired_terminal:
                if snapshot.terminal:
                    self._sessions.pop(key, None)
                    continue
                self._retired_terminal.remove(key)
            if snapshot.terminal:
                if lifecycle.terminal_since is None:
                    lifecycle.terminal_since = now_mono
                elif now_mono - lifecycle.terminal_since >= self.terminal_grace:
                    self._remove_lifecycle(key, lifecycle, "terminal grace expired")
                    continue
            else:
                lifecycle.terminal_since = None
            self._write_snapshot_record(lifecycle, snapshot, state_result)

    def _local_job_ids(self, config_root: Path) -> set[str]:
        jobs = config_root / "jobs"
        try:
            result = set()
            for path in jobs.iterdir():
                if not path.is_dir():
                    continue
                try:
                    result.add(short_session_id(path.name))
                except ValueError:
                    continue
            return result
        except OSError:
            return set()

    def _entry_cwd(
        self,
        config_root: Path,
        live_entry: dict[str, Any] | None,
        state_result: JobStateResult,
    ) -> Path:
        raw = value_as_str(state_result.payload, "cwd") or value_as_str(live_entry, "cwd")
        if raw:
            try:
                return Path(raw).resolve()
            except (OSError, ValueError):
                pass
        return self.cwd or config_root

    def _write_snapshot_record(self, lifecycle, snapshot, state_result: JobStateResult) -> None:
        existing = self.store.load("claude", lifecycle.target_id)
        record = existing or KeeperRecord.new(
            provider="claude",
            target_id=lifecycle.target_id,
            pid=os.getpid(),
            display_name=snapshot.display_name,
            idle_timeout_seconds=0,
            log_path=self.log_path,
            selected_via="all",
        )
        record.pid = os.getpid()
        record.keeper_status = "observed"
        record.display_name = snapshot.display_name
        record.target_status = snapshot.status
        record.loaded = snapshot.loaded
        record.blocked = snapshot.blocked
        record.terminal = snapshot.terminal
        record.last_activity_at = isoformat_or_none(snapshot.last_activity_at)
        record.last_event_at = isoformat_or_none(snapshot.last_event_at)
        record.idle_since = isoformat_or_none(snapshot.idle_since)
        record.provider_metadata = {
            **snapshot.metadata,
            "managed_by_supervisor": True,
            "supervisor_target": "all",
            "source_root": str(lifecycle.config_root),
            "root_fingerprint": root_fingerprint(lifecycle.config_root),
            "state_outcome": state_result.outcome,
        }
        signature = (
            snapshot.status,
            snapshot.loaded,
            snapshot.blocked,
            snapshot.terminal,
            record.last_activity_at,
            state_result.outcome,
            snapshot.display_name,
            snapshot.metadata.get("session_id"),
            snapshot.metadata.get("cwd"),
            snapshot.metadata.get("live_status"),
        )
        changed = signature != lifecycle.signature
        if changed:
            previous = lifecycle.signature[0] if lifecycle.signature else "new"
            lifecycle.signature = signature
            record.event_count += 1
            self._transition_count += 1
            self.logger.info(
                "Claude session %s root=%s transition %s -> %s state=%s",
                lifecycle.short_id,
                lifecycle.config_root,
                previous,
                snapshot.status,
                state_result.outcome,
            )
        if existing is None or changed:
            self.store.save(record)

    def _write_disappeared_record(self, lifecycle: SessionLifecycle) -> None:
        record = self.store.load("claude", lifecycle.target_id)
        if record is None:
            return
        previous = record.target_status
        record.keeper_status = "observed"
        record.target_status = "disappeared"
        record.loaded = False
        record.blocked = False
        record.terminal = False
        record.provider_metadata.update(
            {
                "managed_by_supervisor": True,
                "source_root": str(lifecycle.config_root),
                "disappearance_grace_seconds": self.disappearance_grace,
            }
        )
        lifecycle.signature = ("disappeared", False, False, False, record.last_activity_at, "missing")
        record.event_count += 1
        self._transition_count += 1
        self.store.save(record)
        self.logger.info(
            "Claude session %s root=%s transition %s -> disappeared",
            lifecycle.short_id,
            lifecycle.config_root,
            previous,
        )

    def _remove_lifecycle(self, key, lifecycle: SessionLifecycle, reason: str) -> None:
        self.store.remove("claude", lifecycle.target_id)
        self._sessions.pop(key, None)
        if reason == "terminal grace expired":
            self._retired_terminal.add(key)
        self._transition_count += 1
        self.logger.info(
            "removed Claude session %s root=%s: %s",
            lifecycle.short_id,
            lifecycle.config_root,
            reason,
        )

    def _persist_supervisor(self) -> None:
        self.store.save(self.record)

    def _remove_session_records(self) -> None:
        for lifecycle in list(self._sessions.values()):
            self.store.remove("claude", lifecycle.target_id)
        self._sessions.clear()

    def _wait(self, delay: float) -> None:
        deadline = time.monotonic() + delay
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame) -> None:
            if not self.stop_requested:
                self.logger.info("received signal %s; stopping Claude discovery supervisor", signum)
            self.stop_requested = True

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
