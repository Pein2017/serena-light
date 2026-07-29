"""Transport-neutral, bounded reference-to-symbol presentation.

This module intentionally owns no LSP request, workspace trust, or filesystem
walk.  An owning adapter supplies already-normalized reference locations; a
workspace-facing seam authorizes and classifies each location; and a
document-facing seam supplies one document-symbol response plus immutable
source snapshot for each candidate file.  That division keeps language-specific
containment recovery and external-root policy out of the shared core.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from serena_light.lsp.normalize import (
    ContainmentRecovery,
    Location,
    NormalizedSymbol,
    Position,
    containing_symbol,
    normalize_document_symbols,
)
from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionError, PositionMapper
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    ErrorEnvelope,
    GenerationMetadata,
    ToolEnvelope,
    TruncationMetadata,
    WorkspaceMetadata,
    error,
    success,
)

type RawSymbol = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReferenceRequest:
    """One adapter-owned source occurrence from which to request references."""

    relative_path: str
    position: Position
    workspace: WorkspaceMetadata | None = None
    adapter: AdapterMetadata | None = None
    generations: GenerationMetadata | None = None


@dataclass(frozen=True, slots=True)
class ReferenceTarget:
    """An authorized location and its display identity.

    ``key`` is a resolved, adapter/workspace-owned file identity.  It permits
    this core to request exactly one document-symbol tree for repeated
    locations in the same candidate file without assuming that URI spelling is
    canonical.  ``read_only_external`` is classification metadata only: it
    never changes the active workspace inventory.
    """

    location: Location
    key: str
    display_path: str
    read_only_external: bool

    def __post_init__(self) -> None:
        if not self.key or not self.display_path:
            raise ValueError("reference targets require stable file identities")


@dataclass(frozen=True, slots=True)
class ReferenceDocumentInput:
    """One candidate file's single document-symbol response and source snapshot."""

    uri: str
    snapshot: FileSnapshot
    raw_symbols: Sequence[RawSymbol] | None
    position_encoding: PositionEncoding = PositionEncoding.UTF16
    recover_containment: ContainmentRecovery | None = None

    def __post_init__(self) -> None:
        if not self.uri:
            raise ValueError("reference document uri must be non-empty")


class ReferenceLocationProvider(Protocol):
    """Adapter seam that owns ``textDocument/references`` and normalization."""

    def find_references(self, request: ReferenceRequest) -> Sequence[Location] | ErrorEnvelope: ...


class ReferenceLocationClassifier(Protocol):
    """Workspace seam that authorizes and labels a normalized semantic edge."""

    def classify_reference_location(self, location: Location) -> ReferenceTarget | ErrorEnvelope: ...


class ReferenceDocumentProvider(Protocol):
    """Adapter seam for the one document-symbol tree of an authorized target."""

    def load_reference_document(self, target: ReferenceTarget) -> ReferenceDocumentInput | ErrorEnvelope: ...


class ReferenceNavigationService:
    """Present references without taking transport or workspace ownership."""

    def __init__(
        self,
        locations: ReferenceLocationProvider,
        classifier: ReferenceLocationClassifier,
        documents: ReferenceDocumentProvider,
    ) -> None:
        self._locations = locations
        self._classifier = classifier
        self._documents = documents

    def find_referencing_symbols(
        self,
        request: ReferenceRequest,
        *,
        max_snippet_chars: int = 240,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Return deterministic containing-symbol reference results.

        Each distinct authorized candidate file is loaded at most once.  The
        core never enumerates a workspace, and it returns a typed ``<file>``
        container when a valid location has no safely usable symbol mapping.
        """

        if not _valid_request(request) or max_snippet_chars <= 0 or max_answer_chars <= 0:
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "relative_path, position, max_snippet_chars, or max_answer_chars"},
                workspace=request.workspace,
                adapter=request.adapter,
                generations=request.generations,
            )
        raw_locations = self._locations.find_references(request)
        if isinstance(raw_locations, ErrorEnvelope):
            return raw_locations
        targets: list[ReferenceTarget] = []
        for location in raw_locations:
            if not isinstance(location, Location):
                return error(
                    ErrorCode.INVALID_INPUT,
                    details={"field": "normalized_reference_locations"},
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            classified = self._classifier.classify_reference_location(location)
            if isinstance(classified, ErrorEnvelope):
                return classified
            targets.append(classified)
        return _render_references(
            request,
            targets,
            self._documents,
            max_snippet_chars=max_snippet_chars,
            max_answer_chars=max_answer_chars,
        )


def find_referencing_symbols(
    request: ReferenceRequest,
    locations: Sequence[Location],
    classifier: ReferenceLocationClassifier,
    documents: ReferenceDocumentProvider,
    *,
    max_snippet_chars: int = 240,
    max_answer_chars: int = 12_000,
) -> ToolEnvelope:
    """Functional form for callers that already dispatched the LSP request."""

    class _FixedLocations:
        def find_references(self, _request: ReferenceRequest) -> Sequence[Location] | ErrorEnvelope:
            return locations

    service = ReferenceNavigationService(
        cast(ReferenceLocationProvider, _FixedLocations()),
        classifier,
        documents,
    )
    return service.find_referencing_symbols(
        request, max_snippet_chars=max_snippet_chars, max_answer_chars=max_answer_chars
    )


def _render_references(
    request: ReferenceRequest,
    targets: Sequence[ReferenceTarget],
    documents: ReferenceDocumentProvider,
    *,
    max_snippet_chars: int,
    max_answer_chars: int,
) -> ToolEnvelope:
    unique = _unique_targets(targets)
    trees: dict[str, _DocumentTree] = {}
    for key, target in _targets_by_file(unique).items():
        loaded = documents.load_reference_document(target)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        try:
            trees[key] = _DocumentTree.from_input(loaded)
        except (PositionError, TypeError, ValueError):
            # A usable reference must not disappear merely because an adapter
            # cannot safely map a document tree.  The file-level fallback still
            # preserves the normalized semantic edge.
            trees[key] = _DocumentTree.unavailable(loaded.uri)

    rendered = [_reference_data(target, trees[target.key], max_snippet_chars) for target in unique]
    data = {"relative_path": request.relative_path, "reference_count": len(rendered), "references": rendered}
    bounded, omitted = _bound_references(data, max_answer_chars)
    return success(
        bounded,
        workspace=request.workspace,
        adapter=request.adapter,
        generations=request.generations,
        truncation=TruncationMetadata(omitted > 0, omitted),
    )


@dataclass(frozen=True, slots=True)
class _DocumentTree:
    uri: str
    snapshot: FileSnapshot | None
    mapper: PositionMapper | None
    symbols: tuple[NormalizedSymbol, ...]

    @classmethod
    def from_input(cls, value: ReferenceDocumentInput) -> _DocumentTree:
        return cls(
            uri=value.uri,
            snapshot=value.snapshot,
            mapper=PositionMapper(value.snapshot, value.position_encoding),
            symbols=normalize_document_symbols(
                value.raw_symbols,
                document_uri=value.uri,
                recover_containment=value.recover_containment,
            ),
        )

    @classmethod
    def unavailable(cls, uri: str) -> _DocumentTree:
        return cls(uri=uri, snapshot=None, mapper=None, symbols=())


def _reference_data(target: ReferenceTarget, document: _DocumentTree, max_snippet_chars: int) -> dict[str, Any]:
    location = target.location
    mapped = document.uri == location.uri and document.mapper is not None
    location_data = _raw_location_data(location)
    container = None
    if mapped:
        assert document.mapper is not None
        try:
            location_data = _location_data(document.mapper, location)
        except PositionError:
            mapped = False
        else:
            container = containing_symbol(document.symbols, location)
    data: dict[str, Any] = {
        "path": target.display_path,
        "read_only_external": target.read_only_external,
        "location": location_data,
        "container": _container_data(container),
    }
    snippet = _snippet(document, location, max_snippet_chars) if mapped else None
    if snippet is not None:
        data["snippet"] = snippet[0]
        data["snippet_truncated"] = snippet[1]
    return data


def _location_data(mapper: PositionMapper | None, location: Location) -> dict[str, dict[str, int]]:
    if mapper is None:
        return _raw_location_data(location)
    return {
        "start": _source_position(mapper, location.range.start),
        "end": _source_position(mapper, location.range.end),
    }


def _raw_location_data(location: Location) -> dict[str, dict[str, int]]:
    return {
        "start": {"line": location.range.start.line + 1, "character": location.range.start.character},
        "end": {"line": location.range.end.line + 1, "character": location.range.end.character},
    }


def _source_position(mapper: PositionMapper, value: Position) -> dict[str, int]:
    offset = mapper.lsp_to_text_offset(LspPosition(value.line, value.character))
    line_start = _line_start_offset(mapper.snapshot.text, offset)
    return {
        "line": value.line + 1,
        "column": offset - line_start + 1,
        "text_offset": offset,
        "byte_offset": mapper.text_offset_to_byte_offset(offset),
    }


def _snippet(document: _DocumentTree, location: Location, limit: int) -> tuple[str, bool] | None:
    if document.snapshot is None or document.mapper is None:
        return None
    try:
        offset = document.mapper.lsp_to_text_offset(
            LspPosition(location.range.start.line, location.range.start.character)
        )
    except PositionError:
        return None
    text = document.snapshot.text
    start = _line_start_offset(text, offset)
    end = _line_end_offset(text, offset)
    line = text[start:end]
    if len(line) <= limit:
        return line, False
    relative_offset = offset - start
    left = max(0, min(relative_offset - limit // 2, len(line) - limit))
    right = left + limit
    prefix = "…" if left else ""
    suffix = "…" if right < len(line) else ""
    interior_limit = limit - len(prefix) - len(suffix)
    if interior_limit <= 0:
        return "…"[:limit], True
    left = max(0, min(relative_offset - interior_limit // 2, len(line) - interior_limit))
    right = left + interior_limit
    prefix = "…" if left else ""
    suffix = "…" if right < len(line) else ""
    return f"{prefix}{line[left:right]}{suffix}", True


def _container_data(symbol: NormalizedSymbol | None) -> dict[str, Any]:
    if symbol is None:
        return {"kind": "file", "name_path": "<file>"}
    return {"kind": "symbol", "name_path": "/".join(symbol.name_path), "symbol_kind": symbol.kind}


def _unique_targets(targets: Sequence[ReferenceTarget]) -> list[ReferenceTarget]:
    deduplicated = {
        (target.key, target.location.uri, target.location.range.start, target.location.range.end): target
        for target in targets
    }
    return sorted(deduplicated.values(), key=_target_order_key)


def _targets_by_file(targets: Sequence[ReferenceTarget]) -> dict[str, ReferenceTarget]:
    result: dict[str, ReferenceTarget] = {}
    for target in targets:
        result.setdefault(target.key, target)
    return dict(sorted(result.items()))


def _target_order_key(target: ReferenceTarget) -> tuple[str, str, Position, Position]:
    return (target.display_path, target.location.uri, target.location.range.start, target.location.range.end)


def _bound_references(data: dict[str, Any], max_answer_chars: int) -> tuple[dict[str, Any], int]:
    references = data["references"]
    assert isinstance(references, list)
    kept: list[dict[str, Any]] = []
    for reference in references:
        candidate = {**data, "reference_count": len(kept) + 1, "references": [*kept, reference]}
        if len(_canonical_json(candidate)) > max_answer_chars:
            break
        kept.append(reference)
    return {**data, "reference_count": len(kept), "references": kept}, len(references) - len(kept)


def _valid_request(request: ReferenceRequest) -> bool:
    return (
        _valid_relative_path(request.relative_path)
        and request.position.line >= 0
        and request.position.character >= 0
    )


def _valid_relative_path(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _line_start_offset(text: str, offset: int) -> int:
    return max(text.rfind("\n", 0, offset), text.rfind("\r", 0, offset)) + 1


def _line_end_offset(text: str, offset: int) -> int:
    endings = [index for index in (text.find("\r", offset), text.find("\n", offset)) if index >= 0]
    return min(endings) if endings else len(text)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
