from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import struct
from typing import Literal

Guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClosedError(RuntimeError):
    """Raised when the underlying websocket closes."""


class UnixWebSocket:
    def __init__(
        self,
        socket_path: str,
        *,
        connect_timeout: float = 10.0,
    ) -> None:
        self.socket_path = socket_path
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.connect_timeout)
            sock.connect(self.socket_path)

            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)

            response = self._read_http_response(sock)
            if b"101 Switching Protocols" not in response:
                raise RuntimeError(f"websocket handshake failed: {response!r}")

            accept = self._extract_header(response, "Sec-WebSocket-Accept")
            expected = base64.b64encode(hashlib.sha1(f"{key}{Guid}".encode("ascii")).digest()).decode(
                "ascii"
            )
            if accept != expected:
                raise RuntimeError("websocket accept header did not match request key")

            sock.settimeout(None)
            self._sock = sock
        except BaseException:
            sock.close()
            raise

    def send_text(self, message: str) -> None:
        self._send_frame(0x1, message.encode("utf-8"))

    def send_ping(self, payload: bytes = b"keepalive") -> None:
        self._send_frame(0x9, payload)

    def send_close(self, payload: bytes = b"") -> None:
        try:
            self._send_frame(0x8, payload)
        except OSError:
            return

    def recv_event(
        self,
        *,
        timeout: float | None = None,
    ) -> tuple[Literal["text", "pong", "close"], str | bytes] | None:
        sock = self._require_socket()
        sock.settimeout(timeout)
        while True:
            frame = self._recv_frame()
            if frame is None:
                return None
            opcode, payload, finished = frame
            if opcode == 0x8:
                return ("close", payload)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                return ("pong", payload)
            if opcode not in (0x0, 0x1):
                continue
            message_parts = [payload]
            current_opcode = opcode
            while not finished:
                next_frame = self._recv_frame()
                if next_frame is None:
                    raise WebSocketClosedError("socket closed while reading fragmented frame")
                next_opcode, next_payload, finished = next_frame
                if next_opcode == 0x9:
                    self._send_frame(0xA, next_payload)
                    continue
                if next_opcode == 0x8:
                    return ("close", next_payload)
                if next_opcode != 0x0:
                    raise RuntimeError("unexpected websocket continuation opcode")
                message_parts.append(next_payload)
            if current_opcode != 0x1:
                continue
            return ("text", b"".join(message_parts).decode("utf-8"))

    def close(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        finally:
            self._sock = None
            sock.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._require_socket()
        mask = secrets.token_bytes(4)
        masked_payload = bytes(
            byte ^ mask[index % len(mask)]
            for index, byte in enumerate(payload)
        )
        header = bytearray([0x80 | opcode])
        payload_length = len(payload)
        if payload_length < 126:
            header.append(0x80 | payload_length)
        elif payload_length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", payload_length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", payload_length))
        header.extend(mask)
        sock.sendall(bytes(header) + masked_payload)

    def _recv_frame(self) -> tuple[int, bytes, bool] | None:
        first_two = self._recv_exact(2)
        if first_two is None:
            return None
        first, second = first_two
        finished = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        payload_length = second & 0x7F
        if payload_length == 126:
            extended = self._recv_exact(2)
            if extended is None:
                raise WebSocketClosedError("socket closed while reading frame length")
            payload_length = struct.unpack("!H", extended)[0]
        elif payload_length == 127:
            extended = self._recv_exact(8)
            if extended is None:
                raise WebSocketClosedError("socket closed while reading frame length")
            payload_length = struct.unpack("!Q", extended)[0]

        masking_key = b""
        if masked:
            masking_key = self._recv_exact(4) or b""
        payload = self._recv_exact(payload_length)
        if payload is None:
            raise WebSocketClosedError("socket closed while reading frame payload")
        if masked:
            payload = bytes(
                byte ^ masking_key[index % len(masking_key)]
                for index, byte in enumerate(payload)
            )
        return opcode, payload, finished

    def _read_http_response(self, sock: socket.socket) -> bytes:
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        return bytes(response)

    def _extract_header(self, response: bytes, header_name: str) -> str:
        prefix = f"{header_name}:".lower()
        for line in response.decode("latin1").split("\r\n"):
            if line.lower().startswith(prefix):
                return line.split(":", 1)[1].strip()
        raise RuntimeError(f"missing {header_name} header in websocket handshake response")

    def _recv_exact(self, size: int) -> bytes | None:
        sock = self._require_socket()
        if size == 0:
            return b""
        buffer = bytearray()
        while len(buffer) < size:
            try:
                chunk = sock.recv(size - len(buffer))
            except socket.timeout:
                if not buffer:
                    return None
                raise
            if not chunk:
                if not buffer:
                    return None
                raise WebSocketClosedError("socket closed before expected bytes arrived")
            buffer.extend(chunk)
        return bytes(buffer)

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise RuntimeError("websocket is not connected")
        return self._sock
