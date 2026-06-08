from __future__ import annotations

import json
import queue
import threading
from typing import Any

from .ws import UnixWebSocket
from .ws import WebSocketClosedError


class RpcConnectionClosed(RuntimeError):
    """Raised when the app-server connection closes unexpectedly."""


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class JsonRpcClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._ws = UnixWebSocket(socket_path)
        self._notifications: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._pending: dict[int, queue.Queue[tuple[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._next_request_id = 1
        self._closed_error: BaseException | None = None

    def connect(self) -> None:
        self._ws.connect()
        self._reader = threading.Thread(target=self._reader_loop, name="agent-keepalive-rpc", daemon=True)
        self._reader.start()

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 30.0) -> Any:
        if self._closed_error is not None:
            raise RpcConnectionClosed(str(self._closed_error))
        request_id = self._allocate_request_id()
        response_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue
        self._send({"method": method, "id": request_id, "params": params})
        try:
            status, payload = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"timed out waiting for response to {method}") from exc

        if status == "result":
            return payload
        if status == "error":
            raise JsonRpcError(
                int(payload.get("code", -32000)),
                str(payload.get("message", "JSON-RPC request failed")),
                payload.get("data"),
            )
        raise RpcConnectionClosed(str(payload))

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def get_notification(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def ping(self) -> None:
        self._ws.send_ping()

    def close(self) -> None:
        self._stop.set()
        self._ws.close()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    def _send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"))
        with self._send_lock:
            self._ws.send_text(encoded)

    def _reader_loop(self) -> None:
        try:
            while not self._stop.is_set():
                event = self._ws.recv_event(timeout=1.0)
                if event is None:
                    continue
                kind, payload = event
                if kind == "pong":
                    continue
                if kind == "close":
                    raise RpcConnectionClosed("app-server closed the websocket")
                message = json.loads(str(payload))
                self._dispatch_message(message)
        except BaseException as exc:  # noqa: BLE001
            self._closed_error = exc
            self._fail_pending(exc)
            self._notifications.put(
                {
                    "method": "__connection_closed__",
                    "params": {"reason": str(exc)},
                }
            )

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = int(message["id"])
            with self._pending_lock:
                response_queue = self._pending.pop(request_id, None)
            if response_queue is None:
                return
            if "result" in message:
                response_queue.put(("result", message["result"]))
            else:
                response_queue.put(("error", message["error"]))
            return
        self._notifications.put(message)

    def _fail_pending(self, exc: BaseException) -> None:
        with self._pending_lock:
            pending = self._pending
            self._pending = {}
        for response_queue in pending.values():
            response_queue.put(("closed", exc))
