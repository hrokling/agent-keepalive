from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import sys
import time

from .paths import AppPaths
from .providers import get_provider
from .providers.base import RunConfig
from .providers.base import Snapshot
from .providers.base import should_detach
from .state import KeeperRecord
from .state import StateStore
from .timeparse import isoformat_or_none
from .timeparse import utc_now


PING_INTERVAL = 60.0


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


class Keeper:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.provider = get_provider(config.provider)
        self.paths = AppPaths(config.state_root)
        self.paths.ensure()
        self.store = StateStore(self.paths)
        self.log_path = self.paths.keeper_log_path(config.provider, config.target_id)
        self.logger = configure_logger(self.log_path)
        self.session = self.provider.session(config)
        self.stop_requested = False
        self.stop_reason = "unknown"
        self.record = KeeperRecord.new(
            provider=config.provider,
            target_id=config.target_id,
            pid=os.getpid(),
            display_name=None,
            idle_timeout_seconds=int(config.idle_timeout.total_seconds()),
            log_path=self.log_path,
            selected_via=config.selected_via,
            provider_metadata=dict(config.metadata),
        )

    def run(self) -> int:
        self._install_signal_handlers()
        self._persist_state()
        self.logger.info(
            "starting %s keepalive for target %s with idle timeout %ss via %s",
            self.config.provider,
            self.config.target_id,
            int(self.config.idle_timeout.total_seconds()),
            self.config.selected_via,
        )
        try:
            snapshot = self.session.attach()
            self.record.keeper_status = "attached"
            self._apply_snapshot(snapshot)
            self._persist_state()

            self.logger.info(
                "attached to %s target %s (%s) status=%s",
                self.config.provider,
                self.config.target_id,
                self.record.display_name or "unnamed",
                self.record.target_status,
            )

            last_ping = time.monotonic()
            while not self.stop_requested:
                snapshot = self.session.poll(timeout=1.0)
                self._apply_snapshot(snapshot)
                self._persist_state()
                self.logger.info(
                    "observed %s target %s status=%s terminal=%s blocked=%s",
                    self.config.provider,
                    self.config.target_id,
                    self.record.target_status,
                    self.record.terminal,
                    self.record.blocked,
                )

                if self.record.terminal:
                    self.stop_reason = "target_terminal"
                    self.logger.info("%s target %s is terminal; exiting", self.config.provider, self.config.target_id)
                    return 0

                if time.monotonic() - last_ping >= PING_INTERVAL:
                    self.session.ping()
                    last_ping = time.monotonic()

                if should_detach(
                    snapshot,
                    int(self.config.idle_timeout.total_seconds()),
                    now=utc_now(),
                ):
                    self.stop_reason = "idle_timeout_reached"
                    self.logger.info(
                        "idle timeout reached for %s target %s; detaching",
                        self.config.provider,
                        self.config.target_id,
                    )
                    break

            if self.stop_reason == "unknown":
                self.stop_reason = "signal"
            self._graceful_detach()
            return 0
        except BaseException as exc:  # noqa: BLE001
            self.record.keeper_status = "error"
            self.record.last_error = str(exc)
            self._persist_state()
            self.logger.exception("keeper failed for %s target %s", self.config.provider, self.config.target_id)
            print(f"agent-keepalive error: {exc}", file=sys.stderr)
            return 1
        finally:
            try:
                self.session.close()
            finally:
                if self.record.keeper_status != "error":
                    self.store.remove(self.config.provider, self.config.target_id)

    def _graceful_detach(self) -> None:
        self.record.keeper_status = "stopping"
        self.record.stop_reason = self.stop_reason
        self._persist_state()
        try:
            self.session.detach()
            self.logger.info("detached %s target %s", self.config.provider, self.config.target_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("detach failed for %s target %s: %s", self.config.provider, self.config.target_id, exc)

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        self.record.display_name = snapshot.display_name or self.record.display_name
        self.record.target_status = snapshot.status
        self.record.loaded = snapshot.loaded
        self.record.blocked = snapshot.blocked
        self.record.terminal = snapshot.terminal
        self.record.last_activity_at = isoformat_or_none(snapshot.last_activity_at)
        self.record.last_event_at = isoformat_or_none(snapshot.last_event_at)
        self.record.idle_since = isoformat_or_none(snapshot.idle_since)
        self.record.event_count = snapshot.event_count
        self.record.provider_metadata = {
            **self.record.provider_metadata,
            **snapshot.metadata,
        }

    def _persist_state(self) -> None:
        self.store.save(self.record)

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame) -> None:
            self.stop_requested = True
            self.stop_reason = f"signal_{signal.Signals(signum).name.lower()}"
            self.logger.info("received signal %s; stopping keepalive", signum)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
