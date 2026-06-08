from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import __version__
from .rpc import JsonRpcClient


@dataclass(frozen=True)
class ClientInfo:
    name: str = "agent_keepalive"
    title: str = "Agent Keepalive"
    version: str = __version__


class AppServerClient:
    def __init__(
        self,
        socket_path: str,
        *,
        client_info: ClientInfo | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.client_info = client_info or ClientInfo()
        self.rpc = JsonRpcClient(socket_path)

    def connect(self) -> dict[str, Any]:
        self.rpc.connect()
        initialize_result = self.rpc.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.client_info.name,
                    "title": self.client_info.title,
                    "version": self.client_info.version,
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        self.rpc.notify("initialized", {})
        return initialize_result

    def close(self) -> None:
        self.rpc.close()

    def get_notification(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        return self.rpc.get_notification(timeout=timeout)

    def ping(self) -> None:
        self.rpc.ping()

    def list_threads(self, *, limit: int = 20) -> list[dict[str, Any]]:
        result = self.rpc.request(
            "thread/list",
            {
                "limit": limit,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": False,
            },
        )
        return list(result["data"])

    def list_loaded_threads(self) -> list[str]:
        result = self.rpc.request("thread/loaded/list", {})
        return list(result["data"])

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = self.rpc.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": False,
            },
        )
        return dict(result["thread"])

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        result = self.rpc.request(
            "thread/resume",
            {
                "threadId": thread_id,
            },
        )
        return dict(result)

    def start_thread(self, *, cwd: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = cwd
        result = self.rpc.request("thread/start", params)
        return dict(result)

    def start_turn_text(self, thread_id: str, text: str) -> dict[str, Any]:
        result = self.rpc.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": text,
                        "text_elements": [],
                    }
                ],
            },
            timeout=120.0,
        )
        return dict(result)

    def unsubscribe(self, thread_id: str) -> str:
        result = self.rpc.request(
            "thread/unsubscribe",
            {
                "threadId": thread_id,
            },
        )
        return str(result["status"])
