from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ResolvedTarget:
    provider: str
    target_id: str
    display_name: str | None
    selected_via: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class Snapshot:
    target_id: str
    display_name: str | None
    status: str
    loaded: bool | None
    blocked: bool
    terminal: bool
    last_activity_at: datetime | None
    last_event_at: datetime | None
    idle_since: datetime | None
    event_count: int
    metadata: dict[str, object]


@dataclass(frozen=True)
class RunConfig:
    provider: str
    target_id: str
    idle_timeout: timedelta
    state_root: Path
    selected_via: str
    metadata: dict[str, object]


class ProviderSession(Protocol):
    def attach(self) -> Snapshot:
        ...

    def poll(self, *, timeout: float) -> Snapshot:
        ...

    def ping(self) -> None:
        ...

    def detach(self) -> None:
        ...

    def close(self) -> None:
        ...


class Provider(Protocol):
    name: str

    def resolve(self, args) -> ResolvedTarget:
        ...

    def session(self, config: RunConfig) -> ProviderSession:
        ...

    def live_view(self, records: list) -> dict[str, Snapshot]:
        ...


def should_detach(snapshot: Snapshot, idle_timeout_seconds: int, *, now: datetime) -> bool:
    if snapshot.status == "active":
        return False
    if snapshot.terminal:
        return True
    reference = snapshot.idle_since or snapshot.last_activity_at
    if reference is None:
        return False
    return (now - reference).total_seconds() >= idle_timeout_seconds
