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
MAX_JOB_STATE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ClaudePreflight:
    claude_bin: str
    version: str
    auth_method: str | None
    auth_error: str | None
    config_dir: Path


@dataclass(frozen=True)
class ClaudeDiscoveryResult:
    outcome: str
    entries: list[dict[str, Any]]
    error: str | None = None


@dataclass(frozen=True)
class JobStateResult:
    outcome: str
    payload: dict[str, Any] | None


class ClaudeProvider:
    name = "claude"

    def resolve(self, args) -> ResolvedTarget:
        if getattr(args, "all", False):
            roots = configured_roots(getattr(args, "config_root", None))
            preflight = preflight_claude(
                getattr(args, "claude_bin", "claude"), config_dir=roots[0]
            )
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
                    "config_roots": [str(root) for root in roots],
                    "cwd": str(cwd) if cwd is not None else "",
                },
            )
        roots = configured_roots(getattr(args, "config_root", None))
        preflight = preflight_claude(
            getattr(args, "claude_bin", "claude"), config_dir=roots[0]
        )
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
            if (
                record.provider_metadata.get("managed_by_supervisor")
                or record.target_id == "all"
                or record.provider_metadata.get("mode") == "all"
            ):
                result[record.target_id] = snapshot_from_record(record)
                continue
            claude_bin = str(record.provider_metadata.get("claude_bin", "claude"))
            config_dir = Path(str(record.provider_metadata.get("config_dir", Path.home() / ".claude")))
            cwd_value = str(record.provider_metadata.get("cwd", ""))
            cwd = Path(cwd_value).resolve() if cwd_value else None
            try:
                preflight = preflight_claude(claude_bin, config_dir=config_dir)
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
    configured = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    configured = configured.expanduser().resolve()
    version_result = run_cli(
        [resolved, "--version"],
        cwd=Path.cwd(),
        env=minimal_claude_environment(configured, claude_bin=resolved),
    )
    if version_result.returncode != 0:
        raise RuntimeError(f"Claude version check failed with exit code {version_result.returncode}")
    version = parse_version(version_result.stdout or version_result.stderr)
    if version_tuple(version) < MIN_VERSION:
        raise RuntimeError(f"Claude Code {version} is too old; require at least 2.1.139")

    return ClaudePreflight(
        claude_bin=resolved,
        version=version,
        auth_method=None,
        auth_error=None,
        config_dir=configured,
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
    preflight: ClaudePreflight | None,
    short_id: str,
    cwd: Path,
    state: dict[str, Any] | None,
    live_entry: dict[str, Any] | None,
    state_outcome: str = "ok",
    config_dir: Path | None = None,
    claude_bin: str | None = None,
) -> Snapshot:
    effective_config_dir = preflight.config_dir if preflight is not None else config_dir
    effective_claude_bin = preflight.claude_bin if preflight is not None else (claude_bin or "claude")
    effective_version = preflight.version if preflight is not None else None
    session_id = value_as_str(state, "sessionId") if state else None
    session_id = value_as_str(live_entry, "sessionId") or session_id
    if state_outcome == "invalid":
        status = "state_invalid"
    elif state_outcome == "missing" and live_entry is not None:
        status = "state_missing"
    else:
        status = status_from_payload(state, live_entry)
    updated_at = safe_parse_timestamp(value_as_str(state, "updatedAt")) if state else None
    last_activity_at = updated_at
    terminal = status in TERMINAL_STATES
    blocked = status == "blocked"
    display_name = session_id or short_id
    metadata = {
        "seen": state is not None or live_entry is not None,
        "state_available": state is not None,
        "live_entry_available": live_entry is not None,
        "short_session_id": short_id,
        "session_id": session_id,
        "claude_bin": effective_claude_bin,
        "claude_version": effective_version,
        "config_dir": str(effective_config_dir) if effective_config_dir else "",
        "cwd": str(cwd),
        "live_status": value_as_str(live_entry, "status"),
        "state_age_seconds": state_age_seconds(value_as_str(state, "updatedAt")),
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


def status_from_payload(
    state: dict[str, Any] | None,
    live_entry: dict[str, Any] | None,
) -> str:
    state_name = value_as_str(state, "state")
    live_status = value_as_str(live_entry, "status")
    if state_name in TERMINAL_STATES or state_name == "blocked":
        return state_name
    if in_flight_tasks(state) > 0:
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
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).resolve()
    return discover_claude(claude_bin, config_dir=config_dir, cwd=cwd).entries


def discover_claude(
    claude_bin: str,
    *,
    config_dir: Path,
    cwd: Path | None,
) -> ClaudeDiscoveryResult:
    command = [claude_bin, "agents", "--json"]
    run_cwd = cwd or Path.cwd()
    if cwd is not None:
        command.extend(["--cwd", str(cwd)])
    try:
        result = run_cli(
            command,
            cwd=run_cwd,
            env=minimal_claude_environment(config_dir, claude_bin=claude_bin),
        )
    except subprocess.TimeoutExpired:
        return ClaudeDiscoveryResult(
            outcome="failure",
            entries=[],
            error="Claude discovery timed out after 10 seconds",
        )
    except OSError:
        return ClaudeDiscoveryResult(
            outcome="failure",
            entries=[],
            error="Claude discovery could not start or read its output",
        )
    except UnicodeError:
        return ClaudeDiscoveryResult(
            outcome="invalid_json",
            entries=[],
            error="Claude discovery returned undecodable output",
        )
    if result.returncode != 0:
        return ClaudeDiscoveryResult(
            outcome="failure",
            entries=[],
            error=f"Claude discovery failed with exit code {result.returncode}",
        )
    try:
        entries = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, RecursionError):
        return ClaudeDiscoveryResult(
            outcome="invalid_json",
            entries=[],
            error="Claude discovery returned invalid JSON",
        )
    if not isinstance(entries, list):
        return ClaudeDiscoveryResult(
            outcome="invalid_json",
            entries=[],
            error="Claude discovery JSON was not an array",
        )
    valid_entries = [entry for entry in entries if isinstance(entry, dict)]
    if len(valid_entries) != len(entries):
        return ClaudeDiscoveryResult(
            outcome="invalid_json",
            entries=[],
            error="Claude discovery array contained invalid entries",
        )
    if not all(discovery_entry_is_valid(entry) for entry in valid_entries):
        return ClaudeDiscoveryResult(
            outcome="invalid_json",
            entries=[],
            error="Claude discovery array contained malformed session data",
        )
    return ClaudeDiscoveryResult(
        outcome="success" if valid_entries else "empty",
        entries=valid_entries,
    )


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
    state = read_job_state(config_dir, short_id).payload
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
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def read_job_state(config_dir: Path, short_id: str) -> JobStateResult:
    path = jobs_dir(config_dir) / short_id / "state.json"
    try:
        if path.stat().st_size > MAX_JOB_STATE_BYTES:
            return JobStateResult("invalid", None)
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read(MAX_JOB_STATE_BYTES + 1)
    except FileNotFoundError:
        return JobStateResult("missing", None)
    except (OSError, UnicodeError):
        return JobStateResult("invalid", None)
    if len(raw) > MAX_JOB_STATE_BYTES:
        return JobStateResult("invalid", None)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return JobStateResult("invalid", None)
    if not isinstance(payload, dict):
        return JobStateResult("invalid", None)
    for key in ("state", "updatedAt", "sessionId", "cwd"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            return JobStateResult("invalid", None)
    updated_at = payload.get("updatedAt")
    if isinstance(updated_at, str) and safe_parse_timestamp(updated_at) is None:
        return JobStateResult("invalid", None)
    in_flight = payload.get("inFlight")
    if in_flight is not None:
        if not isinstance(in_flight, dict):
            return JobStateResult("invalid", None)
        tasks = in_flight.get("tasks", 0)
        if isinstance(tasks, bool) or not isinstance(tasks, (int, float)):
            return JobStateResult("invalid", None)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and "\x00" in cwd:
        return JobStateResult("invalid", None)
    payload["short_id"] = short_id
    return JobStateResult("ok", payload)


def short_session_id(value: str) -> str:
    if UUID_RE.match(value):
        return value[:8]
    if len(value) == 8 and re.fullmatch(r"[0-9a-fA-F]{8}", value):
        return value
    raise ValueError("Claude session must be a full UUID or 8-character short id")


def parse_version(raw: str) -> str:
    match = VERSION_RE.search(raw)
    if not match:
        raise RuntimeError("could not parse Claude Code version output")
    return ".".join(match.groups())


def version_tuple(raw: str) -> tuple[int, int, int]:
    parts = raw.split(".")
    return int(parts[0]), int(parts[1]), int(parts[2])


def state_age_seconds(raw: str | None) -> float | None:
    parsed = safe_parse_timestamp(raw)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())


def safe_parse_timestamp(raw: str | None) -> datetime | None:
    try:
        return parse_timestamp(raw)
    except (TypeError, ValueError):
        return None


def in_flight_tasks(state: dict[str, Any] | None) -> float:
    in_flight = state.get("inFlight") if isinstance(state, dict) else None
    if not isinstance(in_flight, dict):
        return 0
    tasks = in_flight.get("tasks", 0)
    if isinstance(tasks, bool) or not isinstance(tasks, (int, float)):
        return 0
    return tasks


def discovery_entry_is_valid(entry: dict[str, Any]) -> bool:
    identity = (
        value_as_str(entry, "shortSessionId")
        or value_as_str(entry, "daemonShort")
        or value_as_str(entry, "sessionId")
    )
    if identity is None:
        return False
    try:
        short_session_id(identity)
    except ValueError:
        return False
    for key in ("cwd", "status"):
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            return False
    return "\x00" not in (value_as_str(entry, "cwd") or "")


def value_as_str(payload: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) else None


def run_cli(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
    )


def configured_roots(values: list[str] | None = None) -> list[Path]:
    raw_values = values or []
    if not raw_values:
        from_env = os.environ.get("AGENT_KEEPALIVE_CLAUDE_CONFIG_ROOTS", "")
        raw_values = [value for value in from_env.split(os.pathsep) if value]
    if not raw_values:
        raw_values = [str(Path.home() / ".claude")]
    return list(dict.fromkeys(Path(value).expanduser().resolve() for value in raw_values))


def minimal_claude_environment(config_dir: Path, *, claude_bin: str) -> dict[str, str]:
    resolved = Path(claude_bin)
    inherited_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    path_value = inherited_path
    if resolved.is_absolute() and str(resolved.parent) not in inherited_path.split(os.pathsep):
        path_value = os.pathsep.join([str(resolved.parent), inherited_path])
    return {
        "HOME": str(Path.home()),
        "PATH": path_value,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "CLAUDE_CONFIG_DIR": str(config_dir.expanduser().resolve()),
    }


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
