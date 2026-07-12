from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from ..activity import ThreadActivityTracker
from ..app_server import AppServerClient
from ..paths import default_socket_path
from ..state import KeeperRecord
from ..timeparse import utc_now
from .base import ResolvedTarget
from .base import RunConfig
from .base import Snapshot
from .codex_recovery import CodexAppServerState
from .codex_recovery import ensure_current_codex_app_server


def discover_socket_path(configured: str | None = None) -> Path:
    if configured:
        return Path(configured).expanduser()
    default = default_socket_path()
    if default.exists():
        return default
    socket_dir = Path.home() / ".codex" / "app-server-control"
    candidates = sorted(socket_dir.glob("*.sock"), key=lambda path: path.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    return default


class CodexProvider:
    name = "codex"

    def resolve(self, args) -> ResolvedTarget:
        socket_path = discover_socket_path(getattr(args, "socket", None))
        ensure_current_codex_app_server(socket_path)
        with managed_client(socket_path) as client:
            if getattr(args, "last", False):
                thread = select_recent_thread(client)
                selected_via = "last"
            else:
                thread = client.read_thread(args.thread)
                selected_via = "thread"
        thread_id = str(thread["id"])
        display_name = thread.get("name") if isinstance(thread.get("name"), str) else None
        return ResolvedTarget(
            provider=self.name,
            target_id=thread_id,
            display_name=display_name,
            selected_via=selected_via,
            metadata={"socket_path": str(socket_path)},
        )

    def session(self, config: RunConfig) -> "CodexSession":
        socket_path = Path(str(config.metadata["socket_path"]))
        return CodexSession(config.target_id, socket_path)

    def live_view(self, records: list[KeeperRecord]) -> dict[str, Snapshot]:
        by_socket: dict[str, list[KeeperRecord]] = {}
        for record in records:
            socket_path = str(record.provider_metadata.get("socket_path", default_socket_path()))
            by_socket.setdefault(socket_path, []).append(record)

        result: dict[str, Snapshot] = {}
        for socket_path, socket_records in by_socket.items():
            try:
                with managed_client(Path(socket_path)) as client:
                    loaded = set(client.list_loaded_threads())
                    for record in socket_records:
                        thread = client.read_thread(record.target_id)
                        result[record.target_id] = snapshot_from_thread(
                            thread,
                            loaded=record.target_id in loaded,
                            fallback=record,
                        )
            except Exception:
                for record in socket_records:
                    result[record.target_id] = snapshot_from_record(record)
        return result


class CodexSession:
    def __init__(self, thread_id: str, socket_path: Path) -> None:
        self.thread_id = thread_id
        self.socket_path = socket_path
        self.client = AppServerClient(str(socket_path))
        self.tracker = ThreadActivityTracker(thread_id)
        self.app_server_state: CodexAppServerState | None = None
        self.last_snapshot = snapshot_from_tracker(thread_id, self.tracker, self._metadata())

    def attach(self) -> Snapshot:
        self.app_server_state = ensure_current_codex_app_server(self.socket_path)
        self.client.connect()
        resume = self.client.resume_thread(self.thread_id)
        thread = resume["thread"]
        self.tracker.note_thread_snapshot(thread)
        self.last_snapshot = snapshot_from_tracker(
            self.thread_id,
            self.tracker,
            self._metadata(),
        )
        return self.last_snapshot

    def poll(self, *, timeout: float) -> Snapshot:
        notification = self.client.get_notification(timeout=timeout)
        now = utc_now()
        if notification is not None:
            method = notification.get("method")
            if method == "__connection_closed__":
                reason = notification.get("params", {}).get("reason", "connection closed")
                raise RuntimeError(str(reason))
            self.tracker.note_notification(notification, observed_at=now)
        self.last_snapshot = snapshot_from_tracker(
            self.thread_id,
            self.tracker,
            self._metadata(),
        )
        return self.last_snapshot

    def ping(self) -> None:
        self.client.ping()

    def detach(self) -> None:
        self.client.unsubscribe(self.thread_id)

    def close(self) -> None:
        self.client.close()

    def _metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {"socket_path": str(self.socket_path)}
        if self.app_server_state is None:
            return metadata
        metadata["cli_version"] = self.app_server_state.cli_version
        metadata["managed_codex_version"] = self.app_server_state.managed_codex_version
        metadata["app_server_version"] = self.app_server_state.app_server_version
        metadata["app_server_expected_version"] = self.app_server_state.expected_version
        metadata["app_server_recovery_action"] = self.app_server_state.recovery_action
        return metadata


def snapshot_from_tracker(
    thread_id: str,
    tracker: ThreadActivityTracker,
    metadata: dict[str, object],
) -> Snapshot:
    return Snapshot(
        target_id=thread_id,
        display_name=tracker.thread_name,
        status=tracker.status,
        loaded=tracker.loaded,
        blocked=False,
        terminal=tracker.status == "notLoaded",
        last_activity_at=tracker.last_activity_at,
        last_event_at=tracker.last_event_at,
        idle_since=tracker.idle_since,
        event_count=tracker.event_count,
        metadata={
            **metadata,
            "last_notification_method": tracker.last_notification_method,
        },
    )


def snapshot_from_thread(
    thread: dict[str, Any],
    *,
    loaded: bool,
    fallback: KeeperRecord,
) -> Snapshot:
    updated_at = thread.get("updatedAt")
    last_activity_at = None
    if isinstance(updated_at, (int, float)):
        last_activity_at = datetime.fromtimestamp(updated_at, tz=timezone.utc)
    status = _thread_status(thread)
    return Snapshot(
        target_id=str(thread.get("id", fallback.target_id)),
        display_name=thread.get("name") if isinstance(thread.get("name"), str) else fallback.display_name,
        status=status,
        loaded=loaded,
        blocked=False,
        terminal=status == "notLoaded",
        last_activity_at=last_activity_at,
        last_event_at=None,
        idle_since=None if status == "active" else last_activity_at,
        event_count=fallback.event_count,
        metadata=dict(fallback.provider_metadata),
    )


def snapshot_from_record(record: KeeperRecord) -> Snapshot:
    from ..timeparse import parse_timestamp

    return Snapshot(
        target_id=record.target_id,
        display_name=record.display_name,
        status=record.target_status,
        loaded=record.loaded,
        blocked=record.blocked,
        terminal=record.terminal,
        last_activity_at=parse_timestamp(record.last_activity_at),
        last_event_at=parse_timestamp(record.last_event_at),
        idle_since=parse_timestamp(record.idle_since),
        event_count=record.event_count,
        metadata=dict(record.provider_metadata),
    )


def select_recent_thread(client: AppServerClient) -> dict[str, Any]:
    loaded_ids = set(client.list_loaded_threads())
    threads = client.list_threads(limit=20)
    if not threads:
        raise RuntimeError("no threads were returned by thread/list")
    return max(
        threads,
        key=lambda thread: (
            thread.get("id") in loaded_ids or _thread_status(thread) != "notLoaded",
            int(thread.get("updatedAt", 0)),
        ),
    )


def _thread_status(thread: dict[str, Any]) -> str:
    status = thread.get("status")
    if isinstance(status, dict) and isinstance(status.get("type"), str):
        return str(status["type"])
    return "unknown"


class managed_client:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.client: AppServerClient | None = None

    def __enter__(self) -> AppServerClient:
        client = AppServerClient(str(self.socket_path))
        client.connect()
        self.client = client
        return client

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.client is not None:
            self.client.close()
