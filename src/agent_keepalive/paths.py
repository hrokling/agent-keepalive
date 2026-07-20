from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

APP_NAME = "agent-keepalive"


def default_socket_path() -> Path:
    return Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"


def default_state_root() -> Path:
    return Path.home() / ".local" / "state" / APP_NAME


@dataclass(frozen=True)
class AppPaths:
    state_root: Path

    @property
    def keepers_dir(self) -> Path:
        return self.state_root / "keepers"

    @property
    def logs_dir(self) -> Path:
        return self.state_root / "logs"

    def ensure(self) -> None:
        self.keepers_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.keepers_dir.chmod(0o700)
        self.logs_dir.chmod(0o700)

    def keeper_state_path(self, provider: str, target_id: str) -> Path:
        return self.keepers_dir / f"{provider}-{safe_target_id(target_id)}.json"

    def keeper_log_path(self, provider: str, target_id: str) -> Path:
        return self.logs_dir / f"{provider}-{safe_target_id(target_id)}.log"


def safe_target_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
