from __future__ import annotations

import io
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, BinaryIO

import pytest

from serena_light.lsp.client import (
    CONTENT_MODIFIED,
    LspProtocolError,
    LspResponseError,
    SyncLspClient,
    encode_message,
    read_message,
)


def test_message_framing_handles_utf8_and_partial_reads() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"name": "火箭🚀"}}
    encoded = encode_message(payload)

    assert read_message(io.BytesIO(encoded)) == payload


@pytest.mark.parametrize(
    "framed",
    [
        b"Content-Type: application/json\r\n\r\n{}",
        b"Content-Length: nope\r\n\r\n",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"Content-Length: 3\r\n\r\n{}",
        b"Content-Length: 2\r\n\r\n[]",
    ],
)
def test_message_framing_rejects_malformed_or_ambiguous_input(framed: bytes) -> None:
    with pytest.raises((EOFError, LspProtocolError)):
        read_message(io.BytesIO(framed))


@contextmanager
def client_and_server(
    server: Callable[[BinaryIO], None],
    **client_kwargs: Any,
) -> Iterator[SyncLspClient]:
    client_socket, server_socket = socket.socketpair()
    client_stream = client_socket.makefile("rwb", buffering=0)
    server_stream = server_socket.makefile("rwb", buffering=0)
    server_thread = threading.Thread(target=server, args=(server_stream,), daemon=True)
    server_thread.start()
    client = SyncLspClient(client_stream, client_stream, request_timeout=0.5, **client_kwargs)
    client.start()
    try:
        yield client
    finally:
        client.close()
        server_stream.close()
        client_socket.close()
        server_socket.close()
        server_thread.join(timeout=1)


def _write(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(encode_message(payload))
    stream.flush()


def test_request_notification_and_void_params() -> None:
    observed: list[dict[str, Any]] = []

    def server(stream: BinaryIO) -> None:
        request = read_message(stream)
        observed.append(request)
        _write(stream, {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}})
        observed.append(read_message(stream))

    with client_and_server(server) as client:
        assert client.request("initialize", {"rootUri": "file:///repo"}) == {"ok": True}
        client.notify("exit")

    assert observed[0]["params"] == {"rootUri": "file:///repo"}
    assert "params" not in observed[1]


def test_content_modified_retries_only_registered_read_requests() -> None:
    request_count = 0

    def server(stream: BinaryIO) -> None:
        nonlocal request_count
        for _ in range(2):
            request = read_message(stream)
            request_count += 1
            if request_count == 1:
                _write(
                    stream,
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": CONTENT_MODIFIED, "message": "stale"},
                    },
                )
            else:
                _write(stream, {"jsonrpc": "2.0", "id": request["id"], "result": ["ok"]})

    with client_and_server(server) as client:
        client.set_content_modified_retry_methods(["textDocument/definition"])
        assert client.request("textDocument/definition", {}) == ["ok"]
        with pytest.raises(ValueError, match="read methods"):
            client.set_content_modified_retry_methods(["workspace/applyEdit"])

    assert request_count == 2


def test_unregistered_content_modified_is_not_retried() -> None:
    def server(stream: BinaryIO) -> None:
        request = read_message(stream)
        _write(
            stream,
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": CONTENT_MODIFIED, "message": "stale"},
            },
        )

    with client_and_server(server) as client, pytest.raises(LspResponseError) as caught:
        client.request("textDocument/definition", {})
    assert caught.value.code == CONTENT_MODIFIED


def test_server_request_handler_and_unknown_method_response() -> None:
    responses: list[dict[str, Any]] = []

    def server(stream: BinaryIO) -> None:
        _write(stream, {"jsonrpc": "2.0", "id": 40, "method": "workspace/configuration", "params": {}})
        responses.append(read_message(stream))
        _write(stream, {"jsonrpc": "2.0", "id": 41, "method": "unknown/request", "params": {}})
        responses.append(read_message(stream))

    with client_and_server(server, request_handlers={"workspace/configuration": lambda _: [{"ok": True}]}):
        server_done = threading.Event()
        threading.Timer(0.05, server_done.set).start()
        assert server_done.wait(0.5)

    assert responses[0] == {"jsonrpc": "2.0", "id": 40, "result": [{"ok": True}]}
    assert responses[1]["error"]["code"] == -32601


def test_request_timeout_removes_pending_request() -> None:
    release = threading.Event()

    def server(stream: BinaryIO) -> None:
        read_message(stream)
        release.wait(1)

    with client_and_server(server) as client:
        with pytest.raises(TimeoutError, match="timed out"):
            client.request("workspace/symbol", {"query": "x"}, timeout=0.05)
        assert client._pending == {}
        release.set()


def test_terminal_handler_observes_transport_end_exactly_once() -> None:
    terminal: list[BaseException] = []

    def server(stream: BinaryIO) -> None:
        stream.write(b"malformed-header\r\n\r\n")
        stream.flush()

    with client_and_server(server, terminal_handler=terminal.append) as client:
        deadline = threading.Event()
        for _ in range(100):
            if terminal:
                break
            deadline.wait(0.005)
        assert not client.is_running

    assert len(terminal) == 1
    assert "malformed LSP header" in str(terminal[0])
