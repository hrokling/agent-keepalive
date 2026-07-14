from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from ..state import KeeperRecord
from ..timeparse import parse_timestamp
from ..timeparse import utc_now
from .base import ResolvedTarget
from .base import RunConfig
from .base import Snapshot


MIN_VERSION = (2, 1, 139)
TERMINAL_STATES = {"done", "failed", "error", "stopped", "cancelled"}
ACTIVE_STATES = {"active", "running", "working", "in_progress", "busy"}
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class ClaudePreflight:
    claude_bin: str
    version: str
    auth_method: str | None
    auth_error: str | None
    config_dir: Path


class ClaudeProvider:
    name = "claude"

    def resolve(self, args) -> ResolvedTarget:
        if getattr(args, "all", False):
            preflight = preflight_claude(getattr(args, "claude_bin", "claude"))
            cwd = Path(getattr(args, "cwd", "")).expanduser().resolve() if getattr(args, "cwd", None) else None
            return ResolvedTarget(
                provider=self.name,
                target_id="all",
                display_name="all Claude sessions",
                selected_via="all",
                metadata={
                    "mode": "all",
                    "claude_bin": preflight.claude_bin,
                    "claude_version": preflight.version,
                    "auth_method": preflight.auth_method,
                    "auth_error": preflight.auth_error,
                    "config_dir": str(preflight.config_dir),
                    "cwd": str(cwd) if cwd is not None else "",
                },
            )
        preflight = preflight_claude(getattr(args, "claude_bin", "claude"))
        cwd = Path(getattr(args, "cwd", "") or os.getcwd()).resolve()
        if getattr(args, "last", False):
            state = select_last_state(preflight.config_dir, cwd=cwd)
            if state is None:
                raise RuntimeError(f"no Claude job state found under {preflight.config_dir / 'jobs'}")
            short_id = str(state["short_id"])
            selected_via = "last"
        else:
            short_id = short_session_id(args.session)
            state = load_state(preflight.config_dir, short_id)
            selected_via = "session"
            if state is None and UUID_RE.match(args.session):
                state = find_state_by_session_id(preflight.config_dir, args.session)
                if state is not None:
                    short_id = str(state["short_id"])

        snapshot = observe_claude(preflight=preflight, short_id=short_id, cwd=cwd)
        if not snapshot.metadata.get("seen"):
            raise RuntimeError(f"Claude session {short_id} was not found")

        return ResolvedTarget(
            provider=self.name,
            target_id=short_id,
            display_name=snapshot.display_name,
            selected_via=selected_via,
            metadata={
                "claude_bin": preflight.claude_bin,
                "claude_version": preflight.version,
                "auth_method": preflight.auth_method,
                "auth_error": preflight.auth_error,
                "config_dir": str(preflight.config_dir),
                "cwd": str(cwd),
                **snapshot.metadata,
            },
        )

    def session(self, config: RunConfig) -> "ClaudeSession":
        claude_bin = str(config.metadata.get("claude_bin", "claude"))
        config_dir = Path(str(config.metadata.get("config_dir", Path.home() / ".claude")))
        cwd = Path(str(config.metadata.get("cwd", os.getcwd()))).resolve()
        preflight = preflight_claude(claude_bin, config_dir=config_dir)
        return ClaudeSession(preflight=preflight, short_id=config.target_id, cwd=cwd)

    def live_view(self, records: list[KeeperRecord]) -> dict[str, Snapshot]:
        result: dict[str, Snapshot] = {}
        for record in records:
            claude_bin = str(record.provider_metadata.get("claude_bin", "claude"))
            config_dir = Path(str(record.provider_metadata.get("config_dir", Path.home() / ".claude")))
            cwd_value = str(record.provider_metadata.get("cwd", ""))
            cwd = Path(cwd_value).resolve() if cwd_value else None
            try:
                preflight = preflight_claude(claude_bin, config_dir=config_dir)
                if str(record.provider_metadata.get("mode")) == "all" or record.target_id == "all":
                    result[record.target_id] = observe_claude_supervisor(
                        preflight=preflight,
                        cwd=cwd,
                    )
                else:
                    result[record.target_id] = observe_claude(
                        preflight=preflight,
                        short_id=record.target_id,
                        cwd=cwd or Path(os.getcwd()).resolve(),
                    )
            except Exception:
                result[record.target_id] = snapshot_from_record(record)
        return result


class ClaudeSession:
    def __init__(self, *, preflight: ClaudePreflight, short_id: str, cwd: Path) -> None:
        self.preflight = preflight
        self.short_id = short_id
        self.cwd = cwd
        self.last_snapshot: Snapshot | None = None

    def attach(self) -> Snapshot:
        snapshot = observe_claude(preflight=self.preflight, short_id=self.short_id, cwd=self.cwd)
        if not snapshot.metadata.get("seen"):
            raise RuntimeError(f"Claude session {self.short_id} was not found")
        self.last_snapshot = snapshot
        return snapshot

    def poll(self, *, timeout: float) -> Snapshot:
        time.sleep(timeout)
        self.last_snapshot = observe_claude(
            preflight=self.preflight,
            short_id=self.short_id,
            cwd=self.cwd,
        )
        return self.last_snapshot

    def ping(self) -> None:
        return None

    def detach(self) -> None:
        return None

    def close(self) -> None:
        return None


def preflight_claude(claude_bin: str, *, config_dir: Path | None = None) -> ClaudePreflight:
    resolved = shutil.which(claude_bin) or claude_bin
    version_result = run_cli([resolved, "--version"], cwd=Path.cwd())
    if version_result.returncode != 0:
        raise RuntimeError(f"`{resolved} --version` failed: {command_error_text(version_result)}")
    version = parse_version(version_result.stdout or version_result.stderr)
    if version_tuple(version) < MIN_VERSION:
        raise RuntimeError(f"Claude Code {version} is too old; require at least 2.1.139")

    auth_method: str | None = None
    auth_error: str | None = None
    auth_result = run_cli([resolved, "auth", "status", "--text"], cwd=Path.cwd())
    if auth_result.returncode == 0:
        auth_method = parse_auth_method(auth_result.stdout)
    else:
        auth_error = command_error_text(auth_result)

    configured = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return ClaudePreflight(
        claude_bin=resolved,
        version=version,
        auth_method=auth_method,
        auth_error=auth_error,
        config_dir=configured.expanduser().resolve(),
    )


def observe_claude(*, preflight: ClaudePreflight, short_id: str, cwd: Path) -> Snapshot:
    state = load_state(preflight.config_dir, short_id)
    live_entry = find_live_entry(preflight.claude_bin, cwd=cwd, short_id=short_id, state=state)
    return snapshot_from_claude_sources(
        preflight=preflight,
        short_id=short_id,
        cwd=cwd,
        state=state,
        live_entry=live_entry,
    )


def observe_claude_entry(
    *,
    preflight: ClaudePreflight,
    short_id: str,
    cwd: Path,
    live_entry: dict[str, Any],
) -> Snapshot:
    """Observe a session already returned by one discovery poll.

    The discovery supervisor uses this to avoid a second ``claude agents``
    invocation per session and to make its admission decision from the same
    live-entry snapshot that selected the session.
    """
    return snapshot_from_claude_sources(
        preflight=preflight,
        short_id=short_id,
        cwd=cwd,
        state=load_state(preflight.config_dir, short_id),
        live_entry=live_entry,
    )


def snapshot_from_claude_sources(
    *,
    preflight: ClaudePreflight,
    short_id: str,
    cwd: Path,
    state: dict[str, Any] | None,
    live_entry: dict[str, Any] | None,
) -> Snapshot:
    session_id = value_as_str(state, "sessionId") if state else None
    session_id = value_as_str(live_entry, "sessionId") or session_id
    status = status_from_payload(state, live_entry)
    updated_at = parse_timestamp(value_as_str(state, "updatedAt")) if state else None
    last_activity_at = updated_at or utc_now()
    terminal = status in TERMINAL_STATES
    blocked = status == "blocked"
    display_name = value_as_str(state, "name") or value_as_str(live_entry, "name") or session_id or short_id
    in_flight = state.get("inFlight") if isinstance(state, dict) else None
    metadata = {
        "seen": state is not None or live_entry is not None,
        "state_available": state is not None,
        "live_entry_available": live_entry is not None,
        "short_session_id": short_id,
        "session_id": session_id,
        "claude_bin": preflight.claude_bin,
        "claude_version": preflight.version,
        "auth_method": preflight.auth_method,
        "auth_error": preflight.auth_error,
        "config_dir": str(preflight.config_dir),
        "cwd": str(cwd),
        "detail": value_as_str(state, "detail"),
        "tempo": value_as_str(state, "tempo"),
        "needs": value_as_str(state, "needs"),
        "suggested_reply": value_as_str(state, "suggestedReply"),
        "transcript_path": value_as_str(state, "linkScanPath"),
        "live_status": value_as_str(live_entry, "status"),
        "state_age_seconds": state_age_seconds(value_as_str(state, "updatedAt")),
        "in_flight": in_flight if isinstance(in_flight, dict) else None,
    }
    return Snapshot(
        target_id=short_id,
        display_name=display_name,
        status=status,
        loaded=not terminal if metadata["seen"] else None,
        blocked=blocked,
        terminal=terminal,
        last_activity_at=last_activity_at,
        last_event_at=last_activity_at,
        idle_since=None if status == "active" else last_activity_at,
        event_count=0,
        metadata=metadata,
    )


def observe_claude_supervisor(*, preflight: ClaudePreflight, cwd: Path | None) -> Snapshot:
    live_entries = list_live_entries(preflight.claude_bin, cwd=cwd)
    return Snapshot(
        target_id="all",
        display_name="all Claude sessions",
        status="active" if live_entries else "idle",
        loaded=True,
        blocked=False,
        terminal=False,
        last_activity_at=utc_now(),
        last_event_at=utc_now(),
        idle_since=None,
        event_count=len(live_entries),
        metadata={
            "mode": "all",
            "seen": True,
            "claude_bin": preflight.claude_bin,
            "claude_version": preflight.version,
            "auth_method": preflight.auth_method,
            "auth_error": preflight.auth_error,
            "config_dir": str(preflight.config_dir),
            "cwd": str(cwd) if cwd is not None else "",
            "live_sessions": len(live_entries),
        },
    )


def status_from_payload(
    state: dict[str, Any] | None,
    live_entry: dict[str, Any] | None,
) -> str:
    state_name = value_as_str(state, "state")
    live_status = value_as_str(live_entry, "status")
    if state_name in TERMINAL_STATES or state_name == "blocked":
        return state_name
    in_flight = state.get("inFlight") if isinstance(state, dict) else None
    if isinstance(in_flight, dict) and int(in_flight.get("tasks", 0) or 0) > 0:
        return "active"
    if live_status in ACTIVE_STATES:
        return "active"
    if state_name in ACTIVE_STATES:
        return "active"
    return state_name or live_status or "unknown"


def find_live_entry(
    claude_bin: str,
    *,
    cwd: Path,
    short_id: str,
    state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    entries = list_live_entries(claude_bin, cwd=cwd)
    session_id = value_as_str(state, "sessionId") if state else None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_session = value_as_str(entry, "sessionId")
        entry_short = value_as_str(entry, "shortSessionId") or value_as_str(entry, "daemonShort")
        if entry_short == short_id or (entry_session and entry_session.startswith(short_id)):
            return entry
        if session_id and entry_session == session_id:
            return entry
    return None


def list_live_entries(claude_bin: str, *, cwd: Path | None) -> list[dict[str, Any]]:
    command = [claude_bin, "agents", "--json"]
    run_cwd = cwd or Path(os.getcwd()).resolve()
    if cwd is not None:
        command.extend(["--cwd", str(cwd)])
    result = run_cli(command, cwd=run_cwd)
    if result.returncode != 0:
        return []
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def select_last_state(config_dir: Path, *, cwd: Path | None = None) -> dict[str, Any] | None:
    states = []
    for state_path in jobs_dir(config_dir).glob("*/state.json"):
        state = load_json_file(state_path)
        if state is None:
            continue
        state["short_id"] = state_path.parent.name
        if cwd is not None and value_as_str(state, "cwd") != str(cwd):
            continue
        states.append(state)
    if not states:
        return None
    return max(states, key=lambda item: value_as_str(item, "updatedAt") or "")


def load_state(config_dir: Path, short_id: str) -> dict[str, Any] | None:
    state = load_json_file(jobs_dir(config_dir) / short_id / "state.json")
    if state is not None:
        state["short_id"] = short_id
    return state


def find_state_by_session_id(config_dir: Path, session_id: str) -> dict[str, Any] | None:
    for state_path in jobs_dir(config_dir).glob("*/state.json"):
        state = load_json_file(state_path)
        if state is not None and value_as_str(state, "sessionId") == session_id:
            state["short_id"] = state_path.parent.name
            return state
    return None


def jobs_dir(config_dir: Path) -> Path:
    return config_dir / "jobs"


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def short_session_id(value: str) -> str:
    if UUID_RE.match(value):
        return value[:8]
    if len(value) == 8 and re.fullmatch(r"[0-9a-fA-F]{8}", value):
        return value
    raise ValueError("Claude session must be a full UUID or 8-character short id")


def parse_version(raw: str) -> str:
    match = VERSION_RE.search(raw)
    if not match:
        raise RuntimeError(f"could not parse Claude Code version from: {raw!r}")
    return ".".join(match.groups())


def version_tuple(raw: str) -> tuple[int, int, int]:
    parts = raw.split(".")
    return int(parts[0]), int(parts[1]), int(parts[2])


def parse_auth_method(raw: str) -> str | None:
    for line in raw.splitlines():
        if line.lower().startswith("login method:"):
            return line.split(":", 1)[1].strip()
    return None


def state_age_seconds(raw: str | None) -> float | None:
    parsed = parse_timestamp(raw)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())


def value_as_str(payload: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) else None


def run_cli(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def command_error_text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"command failed with exit code {result.returncode}").strip()


def snapshot_from_record(record: KeeperRecord) -> Snapshot:
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
