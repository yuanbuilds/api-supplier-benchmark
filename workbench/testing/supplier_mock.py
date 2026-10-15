"""Loopback-only SSE fixtures. These responses are never real model evidence.

Uses only the Python standard library, makes no outbound requests, and records
only request arrival times. Request bodies, arbitrary headers, and credentials
are neither echoed nor retained. Delays are seconds from arrival, not additive.
The returned usage is deliberately synthetic rather than tokenizer accounting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit


MODES = ("normal", "empty", "reasoning-only", "length", "no-done", "truncated", "rate-limit", "timeout")
MOCK_MODEL = "local-mock-not-real-inference"
MAX_BODY_BYTES = 4 * 1024 * 1024


def _delay(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


class MockHTTPServer(ThreadingHTTPServer):
    """An in-memory arrival ledger, intentionally bound only to 127.0.0.1."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, port=0, mode="normal", *, reasoning_delay=0.04,
                 content_delay=0.12, chunk_delay=0.01, timeout_delay=30.0):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        self.mode = mode
        self.reasoning_delay = _delay("reasoning_delay", reasoning_delay)
        self.content_delay = _delay("content_delay", content_delay)
        self.chunk_delay = _delay("chunk_delay", chunk_delay)
        self.timeout_delay = _delay("timeout_delay", timeout_delay)
        if self.content_delay < self.reasoning_delay:
            raise ValueError("content_delay must be at least reasoning_delay")
        self.stopping = threading.Event()
        self._records: list[dict[str, Any]] = []
        self._records_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), MockHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"

    @property
    def request_count(self) -> int:
        with self._records_lock:
            return len(self._records)

    def records_snapshot(self) -> list[dict[str, Any]]:
        with self._records_lock:
            return [dict(record) for record in self._records]

    def _arrival(self) -> dict[str, Any]:
        wall, monotonic = time.time(), time.monotonic()
        with self._records_lock:
            record = {"sequence": len(self._records) + 1,
                      "arrived_monotonic": monotonic, "arrived_at_unix": wall,
                      "arrived_at_utc": datetime.fromtimestamp(wall, timezone.utc).isoformat(),
                      "mode": self.mode, "simulation": True}
            self._records.append(record)
            return dict(record)

    def _wait_until(self, target: float) -> bool:
        return not self.stopping.wait(max(0.0, target - time.monotonic()))

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Do not log arbitrary client input via exception context.
        pass


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalMockOnly/1.0"
    sys_version = ""
    server: MockHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Mock-Only", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(encoded)
        self.wfile.flush()

    def _error(self, status: int, code: str) -> None:
        self._json(status, {"simulation": True, "error": {
            "message": "Synthetic local mock response; no supplier was called",
            "type": "mock_error", "code": code}})

    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path == "/health":
                self._json(200, {"status": "ok", "simulation": True})
            elif path == "/v1/models":
                self._json(200, {"object": "list", "simulation": True, "data": [{
                    "id": MOCK_MODEL, "object": "model", "created": 0,
                    "owned_by": "local-simulation-only"}]})
            elif path == "/__mock__/arrivals":
                records = self.server.records_snapshot()
                self._json(200, {"simulation": True, "request_count": len(records),
                                 "arrivals": records})
            else:
                self._error(404, "not_found")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    def do_POST(self) -> None:
        try:
            if urlsplit(self.path).path != "/v1/chat/completions":
                self._error(404, "not_found")
                return
            record = self.server._arrival()
            self.connection.settimeout(10.0)
            if self.headers.get("Transfer-Encoding"):
                self._error(400, "unsupported_encoding")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self._error(400, "invalid_request")
                return
            if self.server.mode == "rate-limit":
                self._error(429, "rate_limit_exceeded")
                return
            if self.server.mode == "timeout":
                if self.server._wait_until(record["arrived_monotonic"] + self.server.timeout_delay):
                    self._error(504, "mock_timeout")
                self.close_connection = True
                return
            if body.get("stream") is not True:
                self._error(400, "stream_required")
                return
            self._stream(record)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    def _event(self, payload: dict[str, Any] | str) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream(self, record: dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Mock-Only", "true")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        envelope = {"id": f"chatcmpl-local-mock-{record['sequence']}",
                    "object": "chat.completion.chunk", "model": MOCK_MODEL,
                    "created": int(record["arrived_at_unix"]),
                    "system_fingerprint": "simulation-not-real-inference"}

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            self._event({**envelope, "choices": [{"index": 0, "delta": delta,
                                                  "finish_reason": finish}]})

        chunk({"role": "assistant", "content": ""})
        mode, start = self.server.mode, record["arrived_monotonic"]
        has_body = mode in ("normal", "length", "no-done", "truncated")
        if mode != "empty":
            if not self.server._wait_until(start + self.server.reasoning_delay):
                return
            chunk({"reasoning_content": "Synthetic local mock reasoning."})
        if has_body:
            if not self.server._wait_until(start + self.server.content_delay):
                return
            chunk({"content": "O"})
            if not self.server._wait_until(time.monotonic() + self.server.chunk_delay):
                return
            chunk({"content": "K"})
        if mode == "truncated":
            return  # EOF mid-completion: no finish event, usage, or DONE.
        chunk({}, "length" if mode == "length" else "stop")
        reasoning, content = (0 if mode == "empty" else 3), (2 if has_body else 0)
        self._event({**envelope, "choices": [], "usage": {
            "prompt_tokens": 1, "completion_tokens": reasoning + content,
            "total_tokens": 1 + reasoning + content,
            "completion_tokens_details": {"reasoning_tokens": reasoning}}})
        if mode != "no-done":
            self._event("[DONE]")


def start_mock_server(port=0, mode="normal", *, reasoning_delay=0.04,
                      content_delay=0.12, chunk_delay=0.01, timeout_delay=30.0
                      ) -> tuple[MockHTTPServer, threading.Thread]:
    """Start a fixture; caller must shutdown(), server_close(), then join()."""
    server = MockHTTPServer(port, mode, reasoning_delay=reasoning_delay,
                            content_delay=content_delay, chunk_delay=chunk_delay,
                            timeout_delay=timeout_delay)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02},
                              name="supplier-local-mock-only", daemon=True)
    thread.start()
    return server, thread
