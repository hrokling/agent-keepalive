from __future__ import annotations

import argparse
from datetime import timedelta
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .claude_supervisor import ClaudeDiscoverySupervisor
from .paths import AppPaths
from .paths import default_state_root
from .providers import get_provider
from .providers.claude import short_session_id
from .providers.codex import discover_socket_path
from .providers.base import RunConfig
from .runner import Keeper
from .state import KeeperRecord
from .state import StateStore
from .state import idle_deadline
from .state import process_is_alive
from .timeparse import format_duration
from .timeparse import format_relative_time
from .timeparse import parse_duration
from .timeparse import parse_timestamp


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("agent-keepalive")
    args = parser.parse_args(argv)
    return args.func(args)

def build_parser(prog: str = "agent-keepalive") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Keep remote coding-agent sessions observable and alive on long-lived hosts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a background keepalive process")
    start_providers = start.add_subparsers(dest="provider", required=True)
    add_codex_start(start_providers.add_parser("codex", help="Keep a Codex thread subscribed"))
    add_claude_start(start_providers.add_parser("claude", help="Monitor a Claude Code background session"))

    run = subparsers.add_parser("run", help="Run a foreground keepalive process")
    run_providers = run.add_subparsers(dest="provider", required=True)
    add_codex_run(run_providers.add_parser("codex", help="Run Codex keepalive"))
    add_claude_run(run_providers.add_parser("claude", help="Run Claude Code monitor"))

    stop = subparsers.add_parser("stop", help="Stop one or more keepalive processes")
    stop_group = stop.add_mutually_exclusive_group(required=True)
    stop_group.add_argument("--target", help="Provider target id to stop")
    stop_group.add_argument("--all", action="store_true", help="Stop all keepalive processes")
    stop.add_argument("--provider", choices=["codex", "claude"], help="Provider filter")
    stop.add_argument("--wait", type=float, default=10.0, help="Seconds to wait for graceful shutdown")
    stop.add_argument("--force", action="store_true", help="Use SIGKILL if SIGTERM is not enough")
    stop.add_argument("--state-root", default=str(default_state_root()), help=argparse.SUPPRESS)
    stop.set_defaults(func=command_stop)

    list_cmd = subparsers.add_parser("list", help="List currently running keepalive processes")
    list_cmd.add_argument("--provider", choices=["codex", "claude"], help="Provider filter")
    list_cmd.add_argument("--state-root", default=str(default_state_root()), help=argparse.SUPPRESS)
    list_cmd.set_defaults(func=command_list)

    status = subparsers.add_parser("status", help="Show detailed keepalive status")
    status.add_argument("--provider", choices=["codex", "claude"], help="Provider filter")
    status.add_argument("--target", help="Provider target id to inspect")
    status.add_argument("--state-root", default=str(default_state_root()), help=argparse.SUPPRESS)
    status.set_defaults(func=command_status)

    return parser


def add_common_start(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idle-timeout", default="1h", help="Idle timeout such as 1h, 90m, or 2h30m")
    parser.add_argument("--state-root", default=str(default_state_root()), help=argparse.SUPPRESS)


def add_common_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idle-timeout", default="1h", help="Idle timeout such as 1h, 90m, or 2h30m")
    parser.add_argument("--selected-via", default="target", help=argparse.SUPPRESS)
    parser.add_argument("--state-root", default=str(default_state_root()), help=argparse.SUPPRESS)


def add_codex_start(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--thread", help="Codex thread id to keep alive")
    group.add_argument("--last", action="store_true", help="Pick the most recent loaded or recent thread")
    parser.add_argument("--socket", help="Path to the Codex app-server control socket")
    add_common_start(parser)
    parser.set_defaults(func=command_start, provider="codex")

def add_codex_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--thread", required=True, help="Codex thread id to keep alive")
    parser.add_argument("--socket", help="Path to the Codex app-server control socket")
    add_common_run(parser)
    parser.set_defaults(func=command_run, provider="codex", target_attr="thread")


def add_claude_start(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Claude session UUID or 8-character short id")
    group.add_argument("--last", action="store_true", help="Pick the most recently updated Claude job")
    group.add_argument("--all", action="store_true", help="Discover and supervise all live Claude sessions")
    parser.add_argument("--cwd", help="Claude project working directory")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code executable")
    add_common_start(parser)
    parser.set_defaults(func=command_start, provider="claude")


def add_claude_run(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Claude session UUID or 8-character short id")
    group.add_argument("--all", action="store_true", help="Discover and supervise all live Claude sessions")
    parser.add_argument("--cwd", help="Claude project working directory")
    parser.add_argument("--claude-bin", default="claude", help="Claude Code executable")
    add_common_run(parser)
    parser.set_defaults(func=command_run, provider="claude", target_attr="session")


def command_start(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root)
    store = StateStore(AppPaths(state_root))
    remove_stale_records(store)

    try:
        target = get_provider(args.provider).resolve(args)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to resolve {args.provider} target: {exc}", file=sys.stderr)
        return 1

    existing = store.load(target.provider, target.target_id)
    if existing and process_is_alive(existing.pid):
        print(
            f"{target.provider} target {target.target_id} is already being kept alive by pid {existing.pid}",
            file=sys.stderr,
        )
        return 1
    if existing and not process_is_alive(existing.pid):
        store.remove(target.provider, target.target_id)

    child_env = child_environment()
    log_path = store.paths.keeper_log_path(target.provider, target.target_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = run_command_for_target(args, target.target_id, state_root, target.selected_via, target.metadata)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=os.getcwd(),
            env=child_env,
            start_new_session=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = store.load(target.provider, target.target_id)
        if record and record.pid == process.pid:
            if record.keeper_status == "attached":
                print(f"started keepalive for {target.provider} target {target.target_id}")
                if target.display_name:
                    print(f"name: {target.display_name}")
                print(f"pid: {process.pid}")
                print(f"log: {record.log_path}")
                return 0
            if record.keeper_status == "error":
                break
        if process.poll() is not None:
            break
        time.sleep(0.1)

    tail = tail_file(log_path)
    print("keepalive process exited before it became ready", file=sys.stderr)
    if tail:
        print(tail, file=sys.stderr)
    return 1


def command_run(args: argparse.Namespace) -> int:
    target_id = getattr(args, getattr(args, "target_attr", "target"), None)
    if args.provider == "codex":
        metadata = {"socket_path": str(discover_socket_path(getattr(args, "socket", None)))}
    else:
        if getattr(args, "all", False):
            return ClaudeDiscoverySupervisor(
                claude_bin=args.claude_bin,
                cwd=Path(args.cwd).expanduser().resolve() if args.cwd else None,
                idle_timeout=args.idle_timeout,
                state_root=Path(args.state_root),
            ).run()
        target_id = short_session_id(target_id)
        metadata = {
            "claude_bin": args.claude_bin,
            "cwd": str(Path(args.cwd or os.getcwd()).resolve()),
        }
    config = RunConfig(
        provider=args.provider,
        target_id=target_id,
        idle_timeout=parse_duration(args.idle_timeout),
        state_root=Path(args.state_root),
        selected_via=args.selected_via,
        metadata=metadata,
    )
    return Keeper(config).run()


def command_stop(args: argparse.Namespace) -> int:
    store = StateStore(AppPaths(Path(args.state_root)))
    remove_stale_records(store)

    if args.all:
        targets = store.list(provider=args.provider)
    else:
        if args.provider:
            record = store.load(args.provider, args.target)
            targets = [record] if record is not None else []
        else:
            targets = [record for record in store.list() if record.target_id == args.target]

    if not targets:
        print("no matching keepalive processes found", file=sys.stderr)
        return 1

    for record in targets:
        assert record is not None
        if not process_is_alive(record.pid):
            store.remove(record.provider, record.target_id)
            print(f"removed stale state for {record.provider} target {record.target_id}")
            continue
        os.kill(record.pid, signal.SIGTERM)
        print(f"sent SIGTERM to pid {record.pid} for {record.provider} target {record.target_id}")

    deadline = time.monotonic() + float(args.wait)
    remaining = {
        (record.provider, record.target_id): record
        for record in targets
        if record is not None and process_is_alive(record.pid)
    }
    while remaining and time.monotonic() < deadline:
        finished: list[tuple[str, str]] = []
        for key, record in remaining.items():
            if not process_is_alive(record.pid):
                store.remove(record.provider, record.target_id)
                print(f"stopped keepalive for {record.provider} target {record.target_id}")
                finished.append(key)
        for key in finished:
            remaining.pop(key, None)
        if remaining:
            time.sleep(0.2)

    if args.force and remaining:
        for key, record in list(remaining.items()):
            os.kill(record.pid, signal.SIGKILL)
            print(f"sent SIGKILL to pid {record.pid} for {record.provider} target {record.target_id}")
            remaining.pop(key, None)
        time.sleep(0.2)

    if remaining:
        for record in remaining.values():
            print(
                f"keepalive for {record.provider} target {record.target_id} is still running as pid {record.pid}",
                file=sys.stderr,
            )
        return 1
    return 0


def command_list(args: argparse.Namespace) -> int:
    store = StateStore(AppPaths(Path(args.state_root)))
    remove_stale_records(store)
    records = store.list(provider=args.provider)
    if not records:
        print("no active keepalive processes")
        return 0

    live_view = query_live_view(records)
    header = (
        f"{'PROVIDER':8}  {'TARGET':36}  {'PID':>7}  {'STATUS':10}  {'LOADED':6}  "
        f"{'LAST ACTIVITY':14}  {'IDLE TIMEOUT':12}  NAME"
    )
    print(header)
    for record in records:
        live = live_view.get((record.provider, record.target_id))
        status = live.status if live else record.target_status
        loaded = live.loaded if live else record.loaded
        loaded_label = "yes" if loaded is True else "no" if loaded is False else "?"
        print(
            f"{record.provider:8}  "
            f"{record.target_id:36}  "
            f"{record.pid:7d}  "
            f"{status[:10]:10}  "
            f"{loaded_label:6}  "
            f"{format_relative_time(parse_timestamp(record.last_activity_at)):14}  "
            f"{format_duration(timedelta(seconds=record.idle_timeout_seconds)):12}  "
            f"{(record.display_name or '-')} "
        )
    return 0


def command_status(args: argparse.Namespace) -> int:
    store = StateStore(AppPaths(Path(args.state_root)))
    remove_stale_records(store)

    records = store.list(provider=args.provider)
    if args.target:
        records = [record for record in records if record.target_id == args.target]
    if not records:
        print("no matching active keepalive processes")
        return 0

    live_view = query_live_view(records)
    for index, record in enumerate(records):
        if index:
            print()
        live = live_view.get((record.provider, record.target_id))
        status = live.status if live else record.target_status
        loaded = live.loaded if live else record.loaded
        loaded_label = "yes" if loaded is True else "no" if loaded is False else "?"
        print(f"Provider: {record.provider}")
        print(f"Target: {record.target_id}")
        print(f"Name: {record.display_name or '-'}")
        print(f"PID: {record.pid}")
        print(f"Keeper status: {record.keeper_status}")
        print(f"Target status: {status}")
        print(f"Loaded: {loaded_label}")
        print(f"Blocked: {record.blocked}")
        print(f"Terminal: {record.terminal}")
        print(f"Started: {record.started_at}")
        print(f"Last activity: {record.last_activity_at or '-'}")
        print(f"Last event: {record.last_event_at or '-'}")
        print(f"Idle since: {record.idle_since or '-'}")
        print(f"Idle timeout: {format_duration(timedelta(seconds=record.idle_timeout_seconds))}")
        print(f"Idle deadline: {idle_deadline(record) or '-'}")
        print(f"Event count: {record.event_count}")
        print(f"Log: {record.log_path}")
        if record.provider_metadata:
            print("Provider metadata:")
            for key, value in sorted(record.provider_metadata.items()):
                if value is not None:
                    print(f"  {key}: {value}")
        if record.last_error:
            print(f"Last error: {record.last_error}")
    return 0


def query_live_view(records: list[KeeperRecord]) -> dict[tuple[str, str], object]:
    result: dict[tuple[str, str], object] = {}
    by_provider: dict[str, list[KeeperRecord]] = {}
    for record in records:
        by_provider.setdefault(record.provider, []).append(record)
    for provider_name, provider_records in by_provider.items():
        provider = get_provider(provider_name)
        for target_id, snapshot in provider.live_view(provider_records).items():
            result[(provider_name, target_id)] = snapshot
    return result


def remove_stale_records(store: StateStore) -> None:
    for record in store.list():
        if not process_is_alive(record.pid):
            store.remove(record.provider, record.target_id)


def run_command_for_target(
    args: argparse.Namespace,
    target_id: str,
    state_root: Path,
    selected_via: str,
    metadata: dict[str, object],
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_keepalive",
        "run",
        args.provider,
    ]
    if args.provider == "codex":
        command.extend(["--thread", target_id, "--socket", str(metadata["socket_path"])])
    else:
        if target_id == "all":
            command.append("--all")
        else:
            command.extend(["--session", target_id])
        if metadata.get("cwd"):
            command.extend(["--cwd", str(metadata["cwd"])])
        command.extend(["--claude-bin", str(metadata.get("claude_bin", "claude"))])
    command.extend(
        [
            "--idle-timeout",
            args.idle_timeout,
            "--state-root",
            str(state_root),
            "--selected-via",
            selected_via,
        ]
    )
    return command


def child_environment() -> dict[str, str]:
    python_path_entries = [str(Path(__file__).resolve().parents[1])]
    if os.environ.get("PYTHONPATH"):
        python_path_entries.append(os.environ["PYTHONPATH"])
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(python_path_entries)
    return child_env


def tail_file(path: Path, *, lines: int = 20) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        content = handle.readlines()
    return "".join(content[-lines:]).rstrip()
