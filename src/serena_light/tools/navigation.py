"""Bounded, document-scoped semantic navigation.

This module deliberately has no workspace, transport, or language-server
lifetime ownership.  Its caller supplies one already-authorized document and
the response to one ``textDocument/documentSymbol`` request.  Keeping that
boundary explicit prevents path-scoped tools from silently becoming a
workspace walk and makes every displayed position derive from the same
immutable source snapshot as its file hash and optional body.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from serena_light.lsp.normalize import BodyCompleteness, NormalizedSymbol, Position, Range, normalize_document_symbols
from serena_light.lsp.positions import (
    FileSnapshot,
    LspPosition,
    PositionEncoding,
    PositionError,
    PositionMapper,
    PublicPositionRenderer,
)
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    ErrorEnvelope,
    GenerationMetadata,
    JsonValue,
    ToolEnvelope,
    TruncationMetadata,
    WorkspaceMetadata,
    error,
    from_workspace_error,
    success,
)
from serena_light.workspace.identity import WorkspaceError

type RawSymbol = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DocumentSymbolInput:
    """One authorized LSP document-symbol response and its source snapshot."""

    relative_path: str
    uri: str
    snapshot: FileSnapshot
    raw_symbols: Sequence[RawSymbol] | None
    position_encoding: PositionEncoding = PositionEncoding.UTF16
    workspace: WorkspaceMetadata | None = None
    adapter: AdapterMetadata | None = None
    generations: GenerationMetadata | None = None
    body_completeness: BodyCompleteness | None = None

    def __post_init__(self) -> None:
        if not self.relative_path or self.relative_path.startswith("/"):
            raise ValueError("relative_path must be a non-empty relative path")
        if not self.uri:
            raise ValueError("document uri must be non-empty")


class DocumentSymbolProvider(Protocol):
    """Authorization/readiness-owning seam for a single selected document.

    Directory scope is a later integration concern: its provider must select
    documents explicitly before invoking this core.  This protocol never
    enumerates a workspace or directory itself.
    """

    def load_document_symbols(self, relative_path: str) -> DocumentSymbolInput: ...


@dataclass(frozen=True, slots=True)
class DocumentNavigation:
    """Normalized tree and immutable source snapshot for exactly one document."""

    relative_path: str
    uri: str
    snapshot: FileSnapshot
    mapper: PositionMapper
    symbols: tuple[NormalizedSymbol, ...]
    workspace: WorkspaceMetadata | None = None
    adapter: AdapterMetadata | None = None
    generations: GenerationMetadata | None = None

    @classmethod
    def from_input(cls, value: DocumentSymbolInput) -> DocumentNavigation:
        """Normalize one response once, before either public operation uses it."""

        return cls(
            relative_path=value.relative_path,
            uri=value.uri,
            snapshot=value.snapshot,
            mapper=PositionMapper(value.snapshot, value.position_encoding),
            symbols=normalize_document_symbols(
                value.raw_symbols,
                document_uri=value.uri,
                body_completeness=value.body_completeness,
            ),
            workspace=value.workspace,
            adapter=value.adapter,
            generations=value.generations,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.snapshot.raw_bytes).hexdigest()


class DocumentNavigationService:
    """Transport-neutral path-scoped tools backed by a one-document provider."""

    def __init__(self, provider: DocumentSymbolProvider) -> None:
        self._provider = provider

    def get_symbols_overview(
        self,
        relative_path: str,
        *,
        max_depth: int = 0,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Return a bounded view of one document-symbol tree."""

        document = self._load(relative_path)
        if isinstance(document, ErrorEnvelope):
            return document
        return get_symbols_overview(document, max_depth=max_depth, max_answer_chars=max_answer_chars)

    def find_symbol(
        self,
        relative_path: str,
        name_path: str | Sequence[str],
        *,
        substring_matching: bool = False,
        include_body: bool = False,
        include_info: bool = False,
        max_answer_chars: int = 12_000,
        _error_max_answer_chars: int | None = None,
    ) -> ToolEnvelope:
        """Resolve one Serena-style name path in one selected document."""

        document = self._load(relative_path)
        if isinstance(document, ErrorEnvelope):
            return document
        return find_symbol(
            document,
            name_path,
            substring_matching=substring_matching,
            include_body=include_body,
            include_info=include_info,
            max_answer_chars=max_answer_chars,
            _error_max_answer_chars=_error_max_answer_chars,
        )

    def find_symbol_in_documents(
        self,
        relative_paths: Sequence[str],
        name_path: str | Sequence[str],
        *,
        relative_scope: str,
        substring_matching: bool = False,
        include_body: bool = False,
        include_info: bool = False,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Search an explicit inventory-selected document set without walking."""

        pattern = _parse_name_path(name_path)
        if (
            pattern is None
            or max_answer_chars <= 0
            or not _valid_relative_scope(relative_scope)
        ):
            return error(ErrorCode.INVALID_INPUT, details={"field": "relative_path, name_path, or max_answer_chars"})
        documents: list[DocumentNavigation] = []
        matches: list[tuple[DocumentNavigation, NormalizedSymbol]] = []
        for relative_path in sorted(set(relative_paths)):
            document = self._load(relative_path)
            if isinstance(document, ErrorEnvelope):
                return document
            documents.append(document)
            symbols = (symbol for root in document.symbols for symbol in root.iter_depth_first())
            matches.extend(
                (document, symbol) for symbol in symbols if _matches_name_path(symbol, pattern, substring_matching)
            )
        matches.sort(key=lambda item: (item[0].relative_path, *_symbol_order_key(item[1])))
        workspace = documents[0].workspace if documents else None
        if not matches:
            return error(
                ErrorCode.SYMBOL_NOT_FOUND,
                details={
                    "relative_path": relative_scope,
                    "name_path": pattern.expression,
                    "scope": "directory",
                },
                workspace=workspace,
            )
        if include_body:
            incomplete = next(
                ((document, symbol) for document, symbol in matches if symbol.body_incomplete_reason is not None),
                None,
            )
            if incomplete is not None:
                document, symbol = incomplete
                return _incomplete_body_error(document, symbol)
        base: dict[str, Any] = {
            "relative_path": relative_scope,
            "scope": "directory",
            "name_path": pattern.expression,
            "symbols": [],
        }
        if len(_canonical_json(base)) > max_answer_chars:
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "max_answer_chars", "minimum_required": len(_canonical_json(base))},
                workspace=workspace,
            )
        kept: list[dict[str, Any]] = []
        for document, symbol in matches:
            rendered = {
                "relative_path": document.relative_path,
                "sha256": document.sha256,
                "symbol": _symbol_data(
                    document,
                    symbol,
                    include_body=include_body,
                    include_info=include_info,
                ),
            }
            if len(_canonical_json({**base, "symbols": [*kept, rendered]})) > max_answer_chars:
                break
            kept.append(rendered)
        omitted = len(matches) - len(kept)
        return success(
            cast(JsonValue, {**base, "symbols": kept}),
            workspace=workspace,
            truncation=TruncationMetadata(omitted > 0, omitted),
        )

    def _load(self, relative_path: str) -> DocumentNavigation | ErrorEnvelope:
        if not _valid_relative_path(relative_path):
            return error(ErrorCode.INVALID_INPUT, details={"field": "relative_path"})
        try:
            loaded = self._provider.load_document_symbols(relative_path)
            if loaded.relative_path != relative_path:
                return error(ErrorCode.INVALID_PATH, details={"path": relative_path})
            return DocumentNavigation.from_input(loaded)
        except WorkspaceError as exc:
            return from_workspace_error(exc)
        except (PositionError, ValueError, TypeError):
            # An authorizer or provider may retain detailed failures internally;
            # the stable tool boundary never serializes an exception string.
            return error(ErrorCode.INVALID_INPUT, details={"path": relative_path})


def get_symbols_overview(
    document: DocumentNavigation,
    *,
    max_depth: int = 0,
    max_answer_chars: int = 12_000,
) -> ToolEnvelope:
    """Render a bounded overview from ``document``'s single normalized tree."""

    if max_depth < 0 or max_answer_chars <= 0:
        return _invalid_bounds(document)
    roots, depth_omitted = _overview_roots(document, max_depth)
    data: dict[str, Any] = {
        "relative_path": document.relative_path,
        "sha256": document.sha256,
        "max_depth": max_depth,
        "depth_truncated": depth_omitted > 0,
        "depth_omitted_count": depth_omitted,
        "symbols": roots,
    }
    bounded, omitted = _bound_overview(data, max_answer_chars)
    return success(
        bounded,
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
        truncation=TruncationMetadata(omitted > 0, omitted),
    )


def find_symbol(
    document: DocumentNavigation,
    name_path: str | Sequence[str],
    *,
    substring_matching: bool = False,
    include_body: bool = False,
    include_info: bool = False,
    max_answer_chars: int = 12_000,
    _error_max_answer_chars: int | None = None,
) -> ToolEnvelope:
    """Resolve one exact or substring Serena-style name path in one document.

    A simple pattern matches a name-path suffix, a relative path matches a
    suffix, and a leading slash requires the full path.  Substring matching is
    intentionally opt-in and applies only to the last pattern segment, as in
    Serena.  A multiple-match result is an ``AMBIGUOUS_SYMBOL`` error rather
    than an arbitrary choice, which makes this safe for a later editing caller
    too.
    """

    pattern = _parse_name_path(name_path)
    if (
        pattern is None
        or max_answer_chars <= 0
        or (_error_max_answer_chars is not None and _error_max_answer_chars <= 0)
    ):
        return _invalid_bounds(document)
    query_text = pattern.expression
    symbols = [symbol for root in document.symbols for symbol in root.iter_depth_first()]
    matches = [symbol for symbol in symbols if _matches_name_path(symbol, pattern, substring_matching)]
    matches.sort(key=_symbol_order_key)
    if not matches:
        return error(
            ErrorCode.SYMBOL_NOT_FOUND,
            details={"relative_path": document.relative_path, "name_path": query_text},
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    if len(matches) > 1:
        candidates, omitted = _bounded_candidates(
            [_symbol_data(document, symbol, include_body=False, include_info=False) for symbol in matches],
            _error_max_answer_chars if _error_max_answer_chars is not None else max_answer_chars,
        )
        return error(
            ErrorCode.AMBIGUOUS_SYMBOL,
            details={
                "relative_path": document.relative_path,
                "name_path": query_text,
                "candidates": candidates,
                "truncated": omitted > 0,
                "omitted_count": omitted,
            },
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    if include_body and matches[0].body_incomplete_reason is not None:
        return _incomplete_body_error(document, matches[0])
    selected = _symbol_data(document, matches[0], include_body=include_body, include_info=include_info)
    if len(_canonical_json(selected)) > max_answer_chars:
        return error(
            ErrorCode.INVALID_INPUT,
            details={"field": "max_answer_chars", "minimum_required": len(_canonical_json(selected))},
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    return success(
        {"relative_path": document.relative_path, "sha256": document.sha256, "symbol": selected},
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _overview_roots(document: DocumentNavigation, max_depth: int) -> tuple[list[dict[str, Any]], int]:
    omitted = 0

    def render(symbol: NormalizedSymbol, depth: int) -> dict[str, Any]:
        nonlocal omitted
        data = _symbol_data(document, symbol, include_body=False, include_info=True)
        if symbol.children and depth >= max_depth:
            omitted += sum(1 for child in symbol.children for _ in child.iter_depth_first())
            data["children"] = []
            data["children_truncated"] = True
        else:
            data["children"] = [render(child, depth + 1) for child in symbol.children]
            data["children_truncated"] = False
        return data

    return [render(root, 0) for root in sorted(document.symbols, key=_symbol_order_key)], omitted


def _bound_overview(data: dict[str, Any], max_answer_chars: int) -> tuple[dict[str, Any], int]:
    roots = data["symbols"]
    assert isinstance(roots, list)
    kept: list[Any] = []
    for root in roots:
        candidate = {**data, "symbols": [*kept, root]}
        if len(_canonical_json(candidate)) > max_answer_chars:
            break
        kept.append(root)
    return {**data, "symbols": kept}, len(roots) - len(kept)


def _bounded_candidates(
    candidates: Sequence[dict[str, Any]],
    max_answer_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    """Keep ambiguity evidence within its caller-owned error budget."""

    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(_canonical_json({"candidates": [*kept, candidate]})) > max_answer_chars:
            break
        kept.append(candidate)
    return kept, len(candidates) - len(kept)


def _symbol_data(
    document: DocumentNavigation,
    symbol: NormalizedSymbol,
    *,
    include_body: bool,
    include_info: bool,
) -> dict[str, Any]:
    renderer = PublicPositionRenderer(document.mapper)
    data: dict[str, Any] = {
        "name": symbol.name,
        "name_path": _name_path_text(symbol),
        "kind": symbol.kind,
        "range": _source_range(renderer, symbol.location.range),
    }
    if include_info:
        data["info"] = {
            "detail": symbol.detail,
            "selection_range": _source_range(renderer, symbol.selection_range),
        }
    if include_body:
        data["body"] = renderer.text(
            _lsp_position(symbol.location.range.start),
            _lsp_position(symbol.location.range.end),
        )
    return data


def _incomplete_body_error(document: DocumentNavigation, symbol: NormalizedSymbol) -> ErrorEnvelope:
    return error(
        ErrorCode.UNSUPPORTED,
        details={
            "operation": "find_symbol",
            "reason": "incomplete_assignment_range",
            "recovery_reason": symbol.body_incomplete_reason,
            "relative_path": document.relative_path,
            "name_path": _name_path_text(symbol),
        },
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _source_range(renderer: PublicPositionRenderer, value: Range) -> dict[str, dict[str, int]]:
    return renderer.range(_lsp_position(value.start), _lsp_position(value.end))


def source_range(mapper: PositionMapper, value: Range) -> dict[str, dict[str, int]]:
    """Render an LSP range with the snapshot-owned source coordinates."""

    return _source_range(PublicPositionRenderer(mapper), value)


def source_body(mapper: PositionMapper, value: Range) -> str:
    """Return the exact decoded source slice for an LSP range."""

    return PublicPositionRenderer(mapper).text(_lsp_position(value.start), _lsp_position(value.end))


@dataclass(frozen=True, slots=True)
class _NamePathPattern:
    components: tuple[str, ...]
    absolute: bool

    @property
    def expression(self) -> str:
        prefix = "/" if self.absolute else ""
        return f"{prefix}{'/'.join(self.components)}"


def _parse_name_path(value: str | Sequence[str]) -> _NamePathPattern | None:
    if isinstance(value, str):
        absolute = value.startswith("/")
        parts = tuple(value.lstrip("/").rstrip("/").split("/"))
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        absolute = False
        parts = tuple(value)
    else:
        return None
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        return None
    return _NamePathPattern(parts, absolute)


def _matches_name_path(
    symbol: NormalizedSymbol,
    pattern: _NamePathPattern,
    substring_matching: bool,
) -> bool:
    """Match exactly the suffix/absolute contract of Serena's matcher."""

    components = symbol.name_path
    if len(components) < len(pattern.components):
        return False
    if pattern.absolute and len(components) != len(pattern.components):
        return False
    suffix = components[-len(pattern.components) :]
    last_index = len(pattern.components) - 1
    return all(
        expected in actual if substring_matching and index == last_index else expected == actual
        for index, (expected, actual) in enumerate(zip(pattern.components, suffix, strict=True))
    )


def _valid_relative_path(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _valid_relative_scope(value: str) -> bool:
    return value == "." or _valid_relative_path(value.rstrip("/"))


def _invalid_bounds(document: DocumentNavigation) -> ToolEnvelope:
    return error(
        ErrorCode.INVALID_INPUT,
        details={"field": "max_depth, max_answer_chars, or name_path"},
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _symbol_order_key(symbol: NormalizedSymbol) -> tuple[int, int, tuple[str, ...]]:
    start = symbol.location.range.start
    return (start.line, start.character, symbol.name_path)


def _name_path_text(symbol: NormalizedSymbol) -> str:
    return "/".join(symbol.name_path)


def _lsp_position(value: Position) -> LspPosition:
    return LspPosition(value.line, value.character)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
