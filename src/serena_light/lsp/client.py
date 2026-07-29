"""Small synchronous JSON-RPC/LSP client core.

The reader thread only frames and dispatches messages. Callers execute blocking
requests from a workspace-owned executor, never from the daemon event loop.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

JSON = dict[str, Any]
CONTENT_MODIFIED = -32801
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
MAX_CONTENT_BYTES = 64 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
READ_ONLY_RETRY_METHODS = frozenset(
    {
        "workspace/symbol",
        "textDocument/documentSymbol",
        "textDocument/hover",
        "textDocument/definition",
        "textDocument/implementation",
        "textDocument/references",
    }
)


class ByteReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def readline(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


class ByteWriter(Protocol):
    def write(self, data: bytes, /) -> int | None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class LspTransportClosed(RuntimeError):
    """Raised when the language-server transport is no longer usable."""


class LspProtocolError(RuntimeError):
    """Raised for malformed framing or JSON-RPC payloads."""


class LspResponseError(RuntimeError):
    """An error object returned by the language server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data


def encode_message(payload: Mapping[str, Any]) -> bytes:
    """Encode one compact UTF-8 JSON-RPC message with LSP framing."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), check_circular=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def _read_exact(stream: ByteReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError(f"LSP body ended with {remaining} byte(s) unread")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: ByteReader, *, max_content_bytes: int = MAX_CONTENT_BYTES) -> JSON:
    """Read one LSP-framed JSON object, rejecting ambiguous or oversized input."""
    content_length: int | None = None
    header_bytes = 0
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("LSP stream closed before a message header")
        header_bytes += len(line)
        if header_bytes > MAX_HEADER_BYTES:
            raise LspProtocolError("LSP headers exceed the bounded limit")
        if line in {b"\n", b"\r\n"}:
            break
        name, separator, raw_value = line.partition(b":")
        if not separator:
            raise LspProtocolError(f"malformed LSP header: {line!r}")
        if name.strip().lower() != b"content-length":
            continue
        if content_length is not None:
            raise LspProtocolError("duplicate Content-Length header")
        try:
            content_length = int(raw_value.strip())
        except ValueError as exc:
            raise LspProtocolError(f"invalid Content-Length: {raw_value.strip()!r}") from exc
    if content_length is None:
        raise LspProtocolError("missing Content-Length header")
    if content_length < 0 or content_length > max_content_bytes:
        raise LspProtocolError(f"Content-Length {content_length} is outside the bounded limit")
    try:
        payload = json.loads(_read_exact(stream, content_length))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LspProtocolError(f"invalid UTF-8 JSON-RPC body: {exc}") from exc
    if not isinstance(payload, dict):
        raise LspProtocolError("JSON-RPC payload must be an object")
    return payload


@dataclass
class _PendingRequest:
    response: queue.Queue[JSON | BaseException]


class SyncLspClient:
    """Thread-safe synchronous request lifecycle over already-open byte streams."""

    def __init__(
        self,
        reader: ByteReader,
        writer: ByteWriter,
        *,
        request_timeout: float = 30.0,
        notification_handler: Callable[[str, Any], None] | None = None,
        request_handlers: Mapping[str, Callable[[Any], Any]] | None = None,
        terminal_handler: Callable[[BaseException], None] | None = None,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._reader = reader
        self._writer = writer
        self._request_timeout = request_timeout
        self._notification_handler = notification_handler
        self._request_handlers = dict(request_handlers or {})
        self._terminal_handler = terminal_handler
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_id = 1
        self._retry_methods: frozenset[str] = frozenset()
        self._terminal_error: BaseException | None = None
        self._closing = False
        self._reader_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._reader_thread is not None and self._reader_thread.is_alive() and self._terminal_error is None

    def start(self) -> None:
        if self._reader_thread is not None:
            raise RuntimeError("LSP client can only be started once")
        self._reader_thread = threading.Thread(target=self._read_loop, name="serena-light-lsp-reader", daemon=True)
        self._reader_thread.start()

    def set_content_modified_retry_methods(self, methods: Iterable[str]) -> None:
        selected = frozenset(methods)
        invalid = selected - READ_ONLY_RETRY_METHODS
        if invalid:
            raise ValueError(f"ContentModified retry is restricted to read methods: {sorted(invalid)}")
        self._retry_methods = selected

    def register_request_handler(self, method: str, handler: Callable[[Any], Any]) -> None:
        if not method:
            raise ValueError("request method must be non-empty")
        self._request_handlers[method] = handler

    def request(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        attempts = 3 if method in self._retry_methods else 1
        for attempt in range(attempts):
            try:
                return self._request_once(method, params, timeout=timeout)
            except LspResponseError as exc:
                if exc.code != CONTENT_MODIFIED or attempt + 1 >= attempts:
                    raise
                time.sleep(0.2)
        raise AssertionError("unreachable ContentModified retry state")

    def _request_once(self, method: str, params: Any, *, timeout: float | None) -> Any:
        if not method:
            raise ValueError("request method must be non-empty")
        with self._pending_lock:
            self._raise_if_terminal()
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest(queue.Queue(maxsize=1))
            self._pending[request_id] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, **_params_field(method, params)})
            wait_seconds = self._request_timeout if timeout is None else timeout
            if wait_seconds <= 0:
                raise ValueError("request timeout must be positive")
            try:
                response = pending.response.get(timeout=wait_seconds)
            except queue.Empty as exc:
                raise TimeoutError(f"LSP request {method!r} timed out after {wait_seconds:g}s") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, BaseException):
            raise response
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                raise LspProtocolError("JSON-RPC error response must contain an object")
            raise LspResponseError(
                int(error.get("code", INTERNAL_ERROR)), str(error.get("message", "")), error.get("data")
            )
        if "result" not in response:
            raise LspProtocolError("JSON-RPC response has neither result nor error")
        return response["result"]

    def notify(self, method: str, params: Any = None) -> None:
        if not method:
            raise ValueError("notification method must be non-empty")
        with self._pending_lock:
            self._raise_if_terminal()
        self._send({"jsonrpc": "2.0", "method": method, **_params_field(method, params)})

    def shutdown(self, *, timeout: float = 2.0) -> None:
        """Attempt the LSP shutdown/exit sequence, then close both streams."""
        if self._terminal_error is None:
            try:
                self.request("shutdown", timeout=timeout)
                self.notify("exit")
            except (LspTransportClosed, LspProtocolError, LspResponseError, TimeoutError):
                pass
        self.close()

    def close(self) -> None:
        self._closing = True
        self._fail_all(LspTransportClosed("LSP client closed"))
        streams = (self._reader,) if self._writer is self._reader else (self._reader, self._writer)
        for stream in streams:
            with suppress(OSError):
                stream.close()
        thread = self._reader_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _raise_if_terminal(self) -> None:
        if self._terminal_error is not None:
            raise LspTransportClosed(str(self._terminal_error)) from self._terminal_error
        if self._closing:
            raise LspTransportClosed("LSP client is closing")

    def _send(self, payload: Mapping[str, Any]) -> None:
        data = encode_message(payload)
        with self._write_lock:
            try:
                self._writer.write(data)
                self._writer.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                terminal = LspTransportClosed(f"failed to write LSP message: {exc}")
                self._fail_all(terminal)
                raise terminal from exc

    def _read_loop(self) -> None:
        terminal: BaseException = LspTransportClosed("LSP client reader stopped")
        try:
            while not self._closing:
                self._dispatch(read_message(self._reader))
        except EOFError as exc:
            terminal = LspTransportClosed(str(exc))
        except BaseException as exc:
            terminal = exc
        self._fail_all(terminal)

    def _dispatch(self, payload: JSON) -> None:
        if "method" in payload:
            method = payload.get("method")
            if not isinstance(method, str) or not method:
                raise LspProtocolError("JSON-RPC method must be a non-empty string")
            if "id" in payload:
                self._handle_server_request(payload, method)
            elif self._notification_handler is not None:
                self._notification_handler(method, payload.get("params"))
            return
        if "id" not in payload:
            raise LspProtocolError("unknown JSON-RPC payload shape")
        response_id = payload["id"]
        if isinstance(response_id, str) and response_id.isdigit():
            response_id = int(response_id)
        if not isinstance(response_id, int):
            raise LspProtocolError("JSON-RPC response id must be an integer")
        with self._pending_lock:
            pending = self._pending.get(response_id)
        if pending is not None:
            pending.response.put(payload)

    def _handle_server_request(self, payload: JSON, method: str) -> None:
        handler = self._request_handlers.get(method)
        if handler is None:
            response: JSON = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": METHOD_NOT_FOUND, "message": f"unsupported server request: {method}"},
            }
        else:
            try:
                response = {"jsonrpc": "2.0", "id": payload["id"], "result": handler(payload.get("params"))}
            except Exception as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": INTERNAL_ERROR, "message": str(exc)},
                }
        self._send(response)

    def _fail_all(self, error: BaseException) -> None:
        with self._pending_lock:
            first_terminal = self._terminal_error is None
            if self._terminal_error is None:
                self._terminal_error = error
            pending = list(self._pending.values())
            self._pending.clear()
        for request in pending:
            with suppress(queue.Full):
                request.response.put_nowait(error)
        if first_terminal and self._terminal_handler is not None:
            with suppress(Exception):
                self._terminal_handler(error)


def _params_field(method: str, params: Any) -> JSON:
    if method in {"shutdown", "exit"}:
        return {}
    return {"params": {} if params is None else params}
