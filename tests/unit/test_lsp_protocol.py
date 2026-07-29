from __future__ import annotations

import pytest
from lsprotocol import types

from serena_light.lsp.protocol import (
    ProtocolError,
    decode_notification,
    decode_request,
    decode_response,
    encode_message,
    request_models,
    supported_notification_methods,
    supported_request_methods,
)


def test_initialize_round_trips_through_lsprotocol_models() -> None:
    request = decode_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "capabilities": {}, "rootUri": "file:///workspace"},
        }
    )

    assert isinstance(request, types.InitializeRequest)
    assert request.params.root_uri == "file:///workspace"
    assert encode_message(request)["params"]["rootUri"] == "file:///workspace"

    response = decode_response("initialize", {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})
    assert isinstance(response, types.InitializeResponse)


@pytest.mark.parametrize(
    ("message", "model"),
    [
        ({"method": "initialized", "params": {}}, types.InitializedNotification),
        ({"method": "exit", "params": None}, types.ExitNotification),
        ({"method": "$/cancelRequest", "params": {"id": 3}}, types.CancelNotification),
        (
            {
                "method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": "file:///a.py", "languageId": "python", "version": 1, "text": "x"}},
            },
            types.DidOpenTextDocumentNotification,
        ),
        (
            {
                "method": "textDocument/didChange",
                "params": {"textDocument": {"uri": "file:///a.py", "version": 2}, "contentChanges": [{"text": "y"}]},
            },
            types.DidChangeTextDocumentNotification,
        ),
        (
            {"method": "textDocument/didClose", "params": {"textDocument": {"uri": "file:///a.py"}}},
            types.DidCloseTextDocumentNotification,
        ),
        (
            {"method": "textDocument/didSave", "params": {"textDocument": {"uri": "file:///a.py"}}},
            types.DidSaveTextDocumentNotification,
        ),
        (
            {"method": "workspace/didChangeConfiguration", "params": {"settings": {}}},
            types.DidChangeConfigurationNotification,
        ),
        (
            {"method": "workspace/didChangeWatchedFiles", "params": {"changes": [{"uri": "file:///a.py", "type": 2}]}},
            types.DidChangeWatchedFilesNotification,
        ),
        (
            {"method": "textDocument/publishDiagnostics", "params": {"uri": "file:///a.py", "diagnostics": []}},
            types.PublishDiagnosticsNotification,
        ),
    ],
)
def test_owned_notifications_decode_to_generated_models(message: dict[str, object], model: type[object]) -> None:
    assert isinstance(decode_notification(message), model)


def test_narrow_registry_maps_tools_to_lsp_methods_without_declaration() -> None:
    requests = supported_request_methods()
    expected_models = {
        "initialize": (types.InitializeRequest, types.InitializeResponse),
        "shutdown": (types.ShutdownRequest, types.ShutdownResponse),
        "workspace/configuration": (types.ConfigurationRequest, types.ConfigurationResponse),
        "workspace/symbol": (types.WorkspaceSymbolRequest, types.WorkspaceSymbolResponse),
        "textDocument/documentSymbol": (types.DocumentSymbolRequest, types.DocumentSymbolResponse),
        "textDocument/hover": (types.HoverRequest, types.HoverResponse),
        "textDocument/definition": (types.DefinitionRequest, types.DefinitionResponse),
        "textDocument/implementation": (types.ImplementationRequest, types.ImplementationResponse),
        "textDocument/references": (types.ReferencesRequest, types.ReferencesResponse),
    }
    assert requests == expected_models.keys()
    assert {
        method: (request_models(method).request, request_models(method).response) for method in requests
    } == expected_models
    assert "textDocument/declaration" not in requests
    assert request_models("textDocument/definition").request is types.DefinitionRequest

    notifications = supported_notification_methods()
    assert {"initialized", "exit", "$/cancelRequest", "textDocument/publishDiagnostics"} <= notifications


@pytest.mark.parametrize(
    "message",
    [
        {"method": "textDocument/declaration", "params": {}},
        {"method": "workspace/executeCommand", "params": {}},
        {"params": {}},
    ],
)
def test_unknown_or_malformed_messages_fail_fast(message: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        decode_request(message)


def test_invalid_supported_message_is_not_silently_accepted() -> None:
    with pytest.raises(ProtocolError, match="invalid LSP request"):
        decode_request({"method": "textDocument/hover", "params": {}})
