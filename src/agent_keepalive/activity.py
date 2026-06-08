from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from .timeparse import utc_now

_MEANINGFUL_PREFIXES = (
    "turn/",
    "item/",
    "hook/",
    "process/",
    "command/exec/",
)
_IGNORED_METHODS = {
    "thread/tokenUsage/updated",
    "remoteControl/status/changed",
    "mcpServer/startupStatus/updated",
}
_ACTIVE_HINT_METHODS = {
    "turn/started",
    "item/started",
}


def _extract_thread_id(message: dict[str, object]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    thread_id = params.get("threadId")
    if isinstance(thread_id, str):
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return str(thread["id"])
    return None


def _status_type(value: object) -> str | None:
    if isinstance(value, dict):
        candidate = value.get("type")
        if isinstance(candidate, str):
            return candidate
    return None


@dataclass
class ThreadActivityTracker:
    thread_id: str
    thread_name: str | None = None
    status: str = "unknown"
    loaded: bool | None = None
    last_activity_at: datetime | None = None
    last_event_at: datetime | None = None
    idle_since: datetime | None = None
    last_notification_method: str | None = None
    event_count: int = 0

    def note_thread_snapshot(
        self,
        thread: dict[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        observed_at = observed_at or utc_now()
        if isinstance(thread.get("name"), str):
            self.thread_name = str(thread["name"])
        updated_at = thread.get("updatedAt")
        if isinstance(updated_at, (int, float)):
            self.last_activity_at = datetime.fromtimestamp(updated_at, tz=timezone.utc)
        status_type = _status_type(thread.get("status"))
        if status_type:
            self._apply_status(status_type, observed_at, from_snapshot=True)

    def note_notification(
        self,
        message: dict[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        observed_at = observed_at or utc_now()
        if _extract_thread_id(message) != self.thread_id:
            return False

        method = message.get("method")
        if not isinstance(method, str):
            return False

        if method in _IGNORED_METHODS:
            return True

        self.event_count += 1
        self.last_event_at = observed_at
        self.last_notification_method = method

        if method == "thread/status/changed":
            params = message.get("params")
            if isinstance(params, dict):
                status_type = _status_type(params.get("status"))
                if status_type:
                    self._apply_status(status_type, observed_at, from_snapshot=False)
            return True

        if method == "thread/closed":
            self.loaded = False
            self.status = "notLoaded"
            if self.idle_since is None:
                self.idle_since = observed_at
            return True

        if method in _ACTIVE_HINT_METHODS:
            self.status = "active"
            self.loaded = True
            self.idle_since = None
            self.last_activity_at = observed_at
            return True

        if method.startswith(_MEANINGFUL_PREFIXES):
            self.last_activity_at = observed_at
            return True

        return True

    def should_detach(
        self,
        idle_timeout_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        if self.status == "active":
            return False
        reference = self.idle_since or self.last_activity_at
        if reference is None:
            return False
        return (now - reference).total_seconds() >= idle_timeout_seconds

    def seconds_until_detach(
        self,
        idle_timeout_seconds: int,
        *,
        now: datetime | None = None,
    ) -> float | None:
        now = now or utc_now()
        if self.status == "active":
            return None
        reference = self.idle_since or self.last_activity_at
        if reference is None:
            return None
        return idle_timeout_seconds - (now - reference).total_seconds()

    def _apply_status(
        self,
        status_type: str,
        observed_at: datetime,
        *,
        from_snapshot: bool,
    ) -> None:
        previous_status = self.status
        self.status = status_type
        self.loaded = status_type != "notLoaded"

        if status_type == "active":
            self.idle_since = None
            if previous_status != "active":
                self.last_activity_at = observed_at
            return

        if previous_status == "active":
            self.last_activity_at = observed_at

        if self.last_activity_at is None:
            self.last_activity_at = observed_at

        if from_snapshot and self.idle_since is None:
            self.idle_since = self.last_activity_at
        else:
            self.idle_since = self.last_activity_at
