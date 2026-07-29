"""The deliberately small LSP wire surface owned by Serena Light.

``lsprotocol`` owns the generated LSP models.  This module only names the
methods that the v1 runtime may send or receive and converts JSON objects to
those models.  In particular, Serena Light's ``find_declaration`` tool is
implemented with ``textDocument/definition``; declaration is not registered
here as an alternative request path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lsprotocol import converters, types


class ProtocolError(ValueError):
    """Raised when a message is outside the owned LSP protocol surface."""


@dataclass(frozen=True)
class RequestModels:
    """The generated request and response types for one allowed method."""

    request: type[Any]
    response: type[Any]


_REQUESTS: dict[str, RequestModels] = {
    "initialize": RequestModels(types.InitializeRequest, types.InitializeResponse),
    "shutdown": RequestModels(types.ShutdownRequest, types.ShutdownResponse),
    "workspace/configuration": RequestModels(types.ConfigurationRequest, types.ConfigurationResponse),
    "workspace/symbol": RequestModels(types.WorkspaceSymbolRequest, types.WorkspaceSymbolResponse),
    "textDocument/documentSymbol": RequestModels(types.DocumentSymbolRequest, types.DocumentSymbolResponse),
    "textDocument/hover": RequestModels(types.HoverRequest, types.HoverResponse),
    # The public find_declaration name deliberately maps to definition.
    "textDocument/definition": RequestModels(types.DefinitionRequest, types.DefinitionResponse),
    "textDocument/implementation": RequestModels(types.ImplementationRequest, types.ImplementationResponse),
    "textDocument/references": RequestModels(types.ReferencesRequest, types.ReferencesResponse),
}

_NOTIFICATIONS: dict[str, type[Any]] = {
    "initialized": types.InitializedNotification,
    "exit": types.ExitNotification,
    "$/cancelRequest": types.CancelNotification,
    "textDocument/didOpen": types.DidOpenTextDocumentNotification,
    "textDocument/didChange": types.DidChangeTextDocumentNotification,
    "textDocument/didSave": types.DidSaveTextDocumentNotification,
    "textDocument/didClose": types.DidCloseTextDocumentNotification,
    "workspace/didChangeConfiguration": types.DidChangeConfigurationNotification,
    "workspace/didChangeWatchedFiles": types.DidChangeWatchedFilesNotification,
    "textDocument/publishDiagnostics": types.PublishDiagnosticsNotification,
}

# Keep a single converter so every caller gets lsprotocol's LSP-specific hooks.
_CONVERTER = converters.get_converter()


def request_models(method: str) -> RequestModels:
    """Return generated models for an owned request method, or fail fast."""

    try:
        return _REQUESTS[method]
    except KeyError as error:
        raise ProtocolError(f"unsupported LSP request method: {method!r}") from error


def notification_model(method: str) -> type[Any]:
    """Return the generated model for an owned notification, or fail fast."""

    try:
        return _NOTIFICATIONS[method]
    except KeyError as error:
        raise ProtocolError(f"unsupported LSP notification method: {method!r}") from error


def decode_request(message: Mapping[str, Any]) -> Any:
    """Structure one supported JSON-RPC request through ``lsprotocol``."""

    method = _method_from(message)
    return _structure(message, request_models(method).request, "request")


def decode_response(method: str, message: Mapping[str, Any]) -> Any:
    """Structure a response for a known request method through ``lsprotocol``."""

    return _structure(message, request_models(method).response, "response")


def decode_notification(message: Mapping[str, Any]) -> Any:
    """Structure one supported JSON-RPC notification through ``lsprotocol``."""

    method = _method_from(message)
    return _structure(message, notification_model(method), "notification")


def encode_message(message: Any) -> dict[str, Any]:
    """Unstructure a generated LSP model into one JSON-RPC object."""

    encoded = _CONVERTER.unstructure(message)
    if not isinstance(encoded, dict):
        raise ProtocolError("LSP model did not encode to a JSON object")
    return encoded


def supported_request_methods() -> frozenset[str]:
    """Return the narrow request registry for capability and dispatch checks."""

    return frozenset(_REQUESTS)


def supported_notification_methods() -> frozenset[str]:
    """Return the narrow notification registry for capability and dispatch checks."""

    return frozenset(_NOTIFICATIONS)


def _method_from(message: Mapping[str, Any]) -> str:
    method = message.get("method")
    if not isinstance(method, str):
        raise ProtocolError("LSP message has no string method")
    return method


def _structure(message: Mapping[str, Any], model: type[Any], kind: str) -> Any:
    try:
        return _CONVERTER.structure(dict(message), model)
    except Exception as error:
        raise ProtocolError(f"invalid LSP {kind}: {error}") from error
