from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any

from ..app_server import AppServerClient


UNMANAGED_RESTART_ERROR = "app server is running but is not managed by codex app-server daemon"
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
UNAVAILABLE_SOCKET_ERRNOS = frozenset(
    {
        errno.ENOENT,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ENOTCONN,
        errno.EPIPE,
    }
)


class CodexRecoveryError(RuntimeError):
    pass


class CodexAppServerUnavailable(CodexRecoveryError):
    """Raised when the Codex app-server cannot currently accept a connection."""

    def __init__(self, socket_path: Path, reason: BaseException | str) -> None:
        self.socket_path = socket_path
        detail = str(reason) or type(reason).__name__
        super().__init__(f"Codex app-server unavailable at {socket_path}: {detail}")


@dataclass(frozen=True)
class CodexDaemonVersion:
    status: str | None
    socket_path: Path | None
    cli_version: str | None
    managed_codex_version: str | None
    app_server_version: str | None


@dataclass(frozen=True)
class CodexAppServerState:
    cli_version: str | None
    managed_codex_version: str | None
    expected_version: str
    app_server_version: str
    recovery_action: str


def ensure_current_codex_app_server(
    socket_path: Path,
    *,
    codex_bin: str = "codex",
) -> CodexAppServerState:
    app_server_version = probe_socket_app_server_version(socket_path)
    daemon = read_daemon_version(codex_bin)
    expected_version = expected_codex_version(daemon)
    validate_daemon_socket_version(daemon, socket_path, app_server_version)

    version_state = classify_version_state(app_server_version, expected_version)
    if version_state == "current":
        return CodexAppServerState(
            cli_version=daemon.cli_version,
            managed_codex_version=daemon.managed_codex_version,
            expected_version=expected_version,
            app_server_version=app_server_version,
            recovery_action="none",
        )

    if daemon.socket_path is not None and daemon.socket_path != socket_path:
        raise CodexRecoveryError(
            f"Codex app-server at {socket_path} is stale ({app_server_version} < {expected_version}), "
            f"but codex daemon manages {daemon.socket_path}; refusing automatic recovery for a different socket"
        )

    recovery_action = recover_stale_socket(socket_path, codex_bin=codex_bin)
    refreshed_daemon = read_daemon_version(codex_bin)
    refreshed_expected_version = expected_codex_version(refreshed_daemon)
    refreshed_app_server_version = probe_socket_app_server_version(socket_path)
    validate_daemon_socket_version(refreshed_daemon, socket_path, refreshed_app_server_version)
    refreshed_state = classify_version_state(refreshed_app_server_version, refreshed_expected_version)
    if refreshed_state != "current":
        raise CodexRecoveryError(
            f"Codex app-server at {socket_path} is still {refreshed_app_server_version} after recovery; "
            f"expected {refreshed_expected_version}"
        )

    return CodexAppServerState(
        cli_version=refreshed_daemon.cli_version,
        managed_codex_version=refreshed_daemon.managed_codex_version,
        expected_version=refreshed_expected_version,
        app_server_version=refreshed_app_server_version,
        recovery_action=recovery_action,
    )


def parse_daemon_version(raw: str) -> CodexDaemonVersion:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexRecoveryError(f"could not parse `codex app-server daemon version` output: {exc}") from exc
    if not isinstance(payload, dict):
        raise CodexRecoveryError("`codex app-server daemon version` did not return a JSON object")

    socket_path = payload.get("socketPath")
    return CodexDaemonVersion(
        status=_value_as_str(payload, "status"),
        socket_path=Path(socket_path).expanduser() if isinstance(socket_path, str) else None,
        cli_version=_value_as_str(payload, "cliVersion"),
        managed_codex_version=_value_as_str(payload, "managedCodexVersion"),
        app_server_version=_value_as_str(payload, "appServerVersion"),
    )


def read_daemon_version(codex_bin: str) -> CodexDaemonVersion:
    result = run_codex_command(codex_bin, ["app-server", "daemon", "version"])
    if result.returncode != 0:
        raise CodexRecoveryError(
            f"`{codex_bin} app-server daemon version` failed: {command_error_text(result)}"
        )
    return parse_daemon_version(result.stdout)


def expected_codex_version(daemon: CodexDaemonVersion) -> str:
    cli_key = version_key_or_none(daemon.cli_version)
    managed_key = version_key_or_none(daemon.managed_codex_version)
    if cli_key is not None and managed_key is not None and cli_key != managed_key:
        raise CodexRecoveryError(
            "Codex CLI version and managed Codex version differ "
            f"({daemon.cli_version} vs {daemon.managed_codex_version}); refusing to guess the current app-server version"
        )
    expected = daemon.managed_codex_version or daemon.cli_version
    if expected is None:
        raise CodexRecoveryError("`codex app-server daemon version` did not report a Codex version")
    version_key(expected)
    return expected


def probe_socket_app_server_version(socket_path: Path) -> str:
    client = AppServerClient(str(socket_path))
    try:
        try:
            initialize_result = client.connect()
        except OSError as exc:
            if is_unavailable_socket_error(exc):
                raise CodexAppServerUnavailable(socket_path, exc) from exc
            raise
    finally:
        client.close()
    version = parse_initialize_version(initialize_result)
    if version is None:
        raise CodexRecoveryError(
            f"could not determine Codex app-server version for socket {socket_path} from initialize payload"
        )
    return version


def is_unavailable_socket_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in UNAVAILABLE_SOCKET_ERRNOS


def parse_initialize_version(payload: dict[str, Any]) -> str | None:
    server_info = payload.get("serverInfo")
    if isinstance(server_info, dict):
        version = server_info.get("version")
        if isinstance(version, str):
            version_key(version)
            return version

    user_agent = payload.get("userAgent")
    if not isinstance(user_agent, str):
        return None
    match = VERSION_RE.search(user_agent)
    if match is None:
        return None
    return ".".join(match.groups())


def validate_daemon_socket_version(
    daemon: CodexDaemonVersion,
    socket_path: Path,
    app_server_version: str,
) -> None:
    if daemon.socket_path != socket_path or daemon.app_server_version is None:
        return
    if version_key(daemon.app_server_version) != version_key(app_server_version):
        raise CodexRecoveryError(
            f"`codex app-server daemon version` reported appServerVersion={daemon.app_server_version}, "
            f"but socket {socket_path} initialized as {app_server_version}"
        )


def classify_version_state(app_server_version: str, expected_version: str) -> str:
    actual_key = version_key(app_server_version)
    expected_key = version_key(expected_version)
    if actual_key == expected_key:
        return "current"
    if actual_key < expected_key:
        return "stale"
    raise CodexRecoveryError(
        f"Codex app-server version {app_server_version} is newer than expected local Codex version {expected_version}"
    )


def recover_stale_socket(socket_path: Path, *, codex_bin: str) -> str:
    restart_result = run_codex_command(codex_bin, ["app-server", "daemon", "restart"])
    if restart_result.returncode == 0:
        return "daemon_restart"

    restart_error = command_error_text(restart_result)
    if UNMANAGED_RESTART_ERROR not in restart_error:
        raise CodexRecoveryError(
            f"`{codex_bin} app-server daemon restart` failed: {restart_error}"
        )

    pid = find_codex_listener_pid(socket_path)
    termination = terminate_process(pid)
    retry_result = run_codex_command(codex_bin, ["app-server", "daemon", "restart"])
    if retry_result.returncode != 0:
        raise CodexRecoveryError(
            f"`{codex_bin} app-server daemon restart` still failed after stopping stale listener pid {pid}: "
            f"{command_error_text(retry_result)}"
        )
    return f"stop_unmanaged_listener_{termination}_then_daemon_restart"


def run_codex_command(codex_bin: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [codex_bin, *args],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )


def command_error_text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"command failed with exit code {result.returncode}").strip()


def version_key(raw: str) -> tuple[int, int, int]:
    match = VERSION_RE.search(raw)
    if match is None:
        raise CodexRecoveryError(f"could not parse Codex version from {raw!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def version_key_or_none(raw: str | None) -> tuple[int, int, int] | None:
    if raw is None:
        return None
    return version_key(raw)


def find_codex_listener_pid(socket_path: Path) -> int:
    inodes = socket_inodes(socket_path)
    if not inodes:
        raise CodexRecoveryError(f"could not find unix socket inode for {socket_path}")

    matches = []
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        try:
            if not process_has_socket_inode(proc_dir, inodes):
                continue
            cmdline = read_cmdline(proc_dir)
        except OSError:
            continue
        if looks_like_codex_listener(cmdline):
            matches.append(pid)

    if not matches:
        raise CodexRecoveryError(
            f"found no codex app-server listener process holding socket {socket_path}"
        )
    if len(matches) > 1:
        match_list = ", ".join(str(pid) for pid in sorted(matches))
        raise CodexRecoveryError(
            f"found multiple codex app-server listener candidates for {socket_path}: {match_list}"
        )
    return matches[0]


def socket_inodes(socket_path: Path) -> set[str]:
    wanted = str(socket_path)
    inodes: set[str] = set()
    with Path("/proc/net/unix").open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.strip().split(maxsplit=7)
            if len(parts) < 8 or parts[-1] != wanted:
                continue
            inodes.add(parts[-2])
    return inodes


def process_has_socket_inode(proc_dir: Path, inodes: set[str]) -> bool:
    fd_dir = proc_dir / "fd"
    for fd_path in fd_dir.iterdir():
        try:
            target = os.readlink(fd_path)
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            return True
    return False


def read_cmdline(proc_dir: Path) -> list[str]:
    raw = (proc_dir / "cmdline").read_bytes()
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\x00") if part]


def looks_like_codex_listener(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    executable = Path(cmdline[0]).name
    return executable == "codex" and "app-server" in cmdline and "--listen" in cmdline


def terminate_process(pid: int) -> str:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_exited"
    if wait_for_process_exit(pid, timeout=5.0):
        return "sigterm"

    os.kill(pid, signal.SIGKILL)
    if wait_for_process_exit(pid, timeout=2.0):
        return "sigkill"
    raise CodexRecoveryError(f"stale codex app-server listener pid {pid} did not exit after SIGKILL")


def wait_for_process_exit(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.1)
    return not process_exists(pid)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _value_as_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None
