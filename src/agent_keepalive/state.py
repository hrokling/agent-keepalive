from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .paths import AppPaths
from .timeparse import isoformat_or_none
from .timeparse import parse_timestamp
from .timeparse import utc_now


@dataclass
class KeeperRecord:
    provider: str
    target_id: str
    pid: int
    display_name: str | None
    started_at: str
    updated_at: str
    last_activity_at: str | None
    last_event_at: str | None
    idle_since: str | None
    idle_timeout_seconds: int
    target_status: str
    loaded: bool | None
    blocked: bool
    terminal: bool
    keeper_status: str
    stop_reason: str | None
    log_path: str
    selected_via: str
    last_error: str | None
    event_count: int
    provider_metadata: dict[str, Any]

    @property
    def idle_timeout_label(self) -> str:
        return f"{self.idle_timeout_seconds}s"

    @classmethod
    def new(
        cls,
        *,
        provider: str,
        target_id: str,
        pid: int,
        display_name: str | None,
        idle_timeout_seconds: int,
        log_path: Path,
        selected_via: str,
        provider_metadata: dict[str, Any] | None = None,
    ) -> "KeeperRecord":
        now = utc_now().isoformat()
        return cls(
            provider=provider,
            target_id=target_id,
            pid=pid,
            display_name=display_name,
            started_at=now,
            updated_at=now,
            last_activity_at=None,
            last_event_at=None,
            idle_since=None,
            idle_timeout_seconds=idle_timeout_seconds,
            target_status="unknown",
            loaded=None,
            blocked=False,
            terminal=False,
            keeper_status="starting",
            stop_reason=None,
            log_path=str(log_path),
            selected_via=selected_via,
            last_error=None,
            event_count=0,
            provider_metadata=provider_metadata or {},
        )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "KeeperRecord":
        metadata = data.get("provider_metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        provider = str(data.get("provider", "codex"))
        target_id = str(data.get("target_id", data.get("thread_id", "")))
        display_name = data.get("display_name", data.get("thread_name"))
        target_status = str(data.get("target_status", data.get("thread_status", "unknown")))
        log_path = str(data.get("log_path", ""))

        legacy_socket = data.get("socket_path")
        if isinstance(legacy_socket, str) and "socket_path" not in metadata:
            metadata = {**metadata, "socket_path": legacy_socket}

        return cls(
            provider=provider,
            target_id=target_id,
            pid=int(data["pid"]),
            display_name=display_name if isinstance(display_name, str) else None,
            started_at=str(data["started_at"]),
            updated_at=str(data["updated_at"]),
            last_activity_at=(
                str(data["last_activity_at"]) if data.get("last_activity_at") is not None else None
            ),
            last_event_at=(
                str(data["last_event_at"]) if data.get("last_event_at") is not None else None
            ),
            idle_since=str(data["idle_since"]) if data.get("idle_since") is not None else None,
            idle_timeout_seconds=int(data["idle_timeout_seconds"]),
            target_status=target_status,
            loaded=bool(data["loaded"]) if data.get("loaded") is not None else None,
            blocked=bool(data.get("blocked", False)),
            terminal=bool(data.get("terminal", False)),
            keeper_status=str(data.get("keeper_status", "unknown")),
            stop_reason=str(data["stop_reason"]) if data.get("stop_reason") is not None else None,
            log_path=log_path,
            selected_via=str(data.get("selected_via", "target")),
            last_error=str(data["last_error"]) if data.get("last_error") is not None else None,
            event_count=int(data.get("event_count", 0)),
            provider_metadata=dict(metadata),
        )

    def touch(self) -> None:
        self.updated_at = utc_now().isoformat()


class StateStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def save(self, record: KeeperRecord) -> None:
        record.touch()
        path = self.paths.keeper_state_path(record.provider, record.target_id)
        self._atomic_write_json(path, asdict(record))

    def load(self, provider: str, target_id: str) -> KeeperRecord | None:
        path = self.paths.keeper_state_path(provider, target_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return KeeperRecord.from_dict(json.load(handle))

    def remove(self, provider: str, target_id: str) -> None:
        path = self.paths.keeper_state_path(provider, target_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def list(self, *, provider: str | None = None) -> list[KeeperRecord]:
        records: list[KeeperRecord] = []
        for path in sorted(self.paths.keepers_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as handle:
                record = KeeperRecord.from_dict(json.load(handle))
            if provider is None or record.provider == provider:
                records.append(record)
        return records

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, path)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def idle_deadline(record: KeeperRecord) -> str | None:
    reference = parse_timestamp(record.idle_since) or parse_timestamp(record.last_activity_at)
    if reference is None:
        return None
    return isoformat_or_none(reference + record_idle_timeout(record))


def record_idle_timeout(record: KeeperRecord):
    from datetime import timedelta

    return timedelta(seconds=record.idle_timeout_seconds)
