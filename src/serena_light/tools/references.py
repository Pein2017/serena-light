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
from serena_light.lsp.positions import (
    FileSnapshot,
    LspPosition,
    PositionEncoding,
    PositionError,
    PositionMapper,
    PublicPositionRenderer,
    raw_lsp_range,
)
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    ErrorEnvelope,
    GenerationMetadata,
    RetryMetadata,
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
class ReferenceCoverage:
    """Bounded evidence for the one native semantic program that was queried.

    The runtime, rather than this presentation layer, owns the projections and
    generation check used to construct this value.  Keeping it immutable here
    prevents a reference result from accidentally combining locations from one
    dispatch with coverage from a later workspace scan.
    """

    adapter: str
    language: str
    scope_kind: str
    configured_program_files: int
    configured_program_digest: str
    trusted_language_files: int
    trusted_language_digest: str
    uncovered_files: int
    uncovered_digest: str
    uncovered_sample: tuple[str, ...]
    uncovered_total: int
    uncovered_omitted: int

    def __post_init__(self) -> None:
        if (
            not self.adapter
            or not self.language
            or not self.scope_kind
            or self.configured_program_files < 0
            or self.trusted_language_files < 0
            or self.uncovered_files < 0
            or self.uncovered_total < 0
            or self.uncovered_omitted < 0
            or self.uncovered_total != self.uncovered_files
            or self.uncovered_omitted != self.uncovered_total - len(self.uncovered_sample)
            or tuple(sorted(self.uncovered_sample)) != self.uncovered_sample
            or len(set(self.uncovered_sample)) != len(self.uncovered_sample)
        ):
            raise ValueError("reference coverage is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "language": self.language,
            "scope_kind": self.scope_kind,
            "configured_program_files": self.configured_program_files,
            "configured_program_digest": self.configured_program_digest,
            "trusted_language_files": self.trusted_language_files,
            "trusted_language_digest": self.trusted_language_digest,
            "uncovered_files": self.uncovered_files,
            "uncovered_sample": {
                "total": self.uncovered_total,
                "items": list(self.uncovered_sample),
                "digest": self.uncovered_digest,
                "omitted": self.uncovered_omitted,
            },
        }


@dataclass(frozen=True, slots=True)
class ReferenceQueryResult:
    """One semantic adapter response paired with its dispatch-time coverage."""

    locations: Sequence[Location]
    coverage: ReferenceCoverage


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


@dataclass(frozen=True, slots=True)
class RawReferenceDocumentInput:
    """One authorized reference target without response-owned source bytes.

    This variant carries only the response adapter's coordinate basis.  It
    cannot supply decoded-text ranges, containment, or snippets, and therefore
    preserves an external semantic edge without inventing a source snapshot or
    acquiring filesystem trust ownership in the presentation layer.
    """

    uri: str
    position_encoding: PositionEncoding

    def __post_init__(self) -> None:
        if not self.uri:
            raise ValueError("raw reference document uri must be non-empty")


type ReferenceDocument = ReferenceDocumentInput | RawReferenceDocumentInput


class ReferenceLocationProvider(Protocol):
    """Adapter seam that owns ``textDocument/references`` and normalization."""

    def find_references(self, request: ReferenceRequest) -> ReferenceQueryResult | ErrorEnvelope: ...


class ReferenceLocationClassifier(Protocol):
    """Workspace seam that authorizes and labels a normalized semantic edge."""

    def classify_reference_location(self, location: Location) -> ReferenceTarget | ErrorEnvelope: ...


class ReferenceDocumentProvider(Protocol):
    """Adapter seam for the one document-symbol tree of an authorized target."""

    def load_reference_document(self, target: ReferenceTarget) -> ReferenceDocument | ErrorEnvelope: ...


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
        max_snippet_chars: int = 0,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Return deterministic containing-symbol reference results.

        Each distinct authorized candidate file is loaded at most once.  The
        core never enumerates a workspace, and it returns a typed ``<file>``
        container when a valid location has no safely usable symbol mapping.
        """

        if not _valid_request(request) or max_snippet_chars < 0 or max_answer_chars <= 0:
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "relative_path, position, max_snippet_chars, or max_answer_chars"},
                workspace=request.workspace,
                adapter=request.adapter,
                generations=request.generations,
            )
        query = self._locations.find_references(request)
        if isinstance(query, ErrorEnvelope):
            return query
        if not isinstance(query, ReferenceQueryResult):
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "reference_query_result"},
                workspace=request.workspace,
                adapter=request.adapter,
                generations=request.generations,
            )
        raw_locations = query.locations
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
            query.coverage,
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
    coverage: ReferenceCoverage,
    *,
    max_snippet_chars: int = 0,
    max_answer_chars: int = 12_000,
) -> ToolEnvelope:
    """Functional form for callers that already dispatched the LSP request."""

    class _FixedLocations:
        def find_references(self, _request: ReferenceRequest) -> ReferenceQueryResult | ErrorEnvelope:
            return ReferenceQueryResult(locations, coverage)

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
    coverage: ReferenceCoverage,
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
        if isinstance(loaded, RawReferenceDocumentInput):
            if not target.read_only_external:
                return _workspace_reference_not_ready(
                    request,
                    "workspace_reference_snapshot_unavailable",
                    target,
                )
            trees[key] = _DocumentTree.unavailable(loaded.uri, loaded.position_encoding)
            continue
        if not isinstance(loaded, ReferenceDocumentInput):
            return _workspace_reference_not_ready(
                request,
                "workspace_reference_document_invalid",
                target,
            )
        if loaded.uri != target.location.uri:
            return _workspace_reference_not_ready(
                request,
                "workspace_reference_uri_mismatch",
                target,
                document_uri=loaded.uri,
            )
        try:
            trees[key] = _DocumentTree.from_input(loaded)
        except (PositionError, TypeError, ValueError):
            return _workspace_reference_not_ready(
                request,
                "workspace_reference_snapshot_unavailable",
                target,
            )

    rendered: list[dict[str, Any]] = []
    for target in unique:
        try:
            rendered.append(_reference_data(target, trees[target.key], max_snippet_chars))
        except PositionError:
            return _workspace_reference_not_ready(
                request,
                "workspace_reference_snapshot_range_mismatch",
                target,
            )
    data = {
        "relative_path": request.relative_path,
        "reference_count": len(rendered),
        "references": rendered,
        "coverage": coverage.to_dict(),
    }
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
    position_encoding: PositionEncoding
    symbols: tuple[NormalizedSymbol, ...]

    @classmethod
    def from_input(cls, value: ReferenceDocumentInput) -> _DocumentTree:
        mapper = PositionMapper(value.snapshot, value.position_encoding)
        try:
            symbols = normalize_document_symbols(
                value.raw_symbols,
                document_uri=value.uri,
                recover_containment=value.recover_containment,
            )
        except Exception:
            # The response-owned source snapshot remains independently valid
            # even if the server's symbol tree is malformed.  Retain its
            # mapping and make containment explicitly empty.
            symbols = ()
        return cls(
            uri=value.uri,
            snapshot=value.snapshot,
            mapper=mapper,
            position_encoding=value.position_encoding,
            symbols=symbols,
        )

    @classmethod
    def unavailable(cls, uri: str, position_encoding: PositionEncoding) -> _DocumentTree:
        return cls(uri=uri, snapshot=None, mapper=None, position_encoding=position_encoding, symbols=())


def _reference_data(target: ReferenceTarget, document: _DocumentTree, max_snippet_chars: int) -> dict[str, Any]:
    location = target.location
    if document.mapper is None:
        if not target.read_only_external:
            raise PositionError("workspace reference mapper is unavailable")
        location_data = _raw_location_data(location, document.position_encoding)
        container = None
        mapped = False
    else:
        if document.uri != location.uri:
            raise PositionError("reference document uri does not match location")
        location_data = _location_data(document.mapper, location)
        container = containing_symbol(document.symbols, location)
        mapped = True
    data: dict[str, Any] = {
        "path": target.display_path,
        "read_only_external": target.read_only_external,
        "location": location_data,
        "container": _container_data(container),
    }
    snippet = _snippet(document, location, max_snippet_chars) if mapped and max_snippet_chars > 0 else None
    if snippet is not None:
        data["snippet"] = snippet[0]
        data["snippet_truncated"] = snippet[1]
    return data


def _workspace_reference_not_ready(
    request: ReferenceRequest,
    reason: str,
    target: ReferenceTarget,
    *,
    document_uri: str | None = None,
) -> ErrorEnvelope:
    details: dict[str, object] = {
        "reason": reason,
        "path": target.display_path,
    }
    if document_uri is not None:
        details["location_uri"] = target.location.uri
        details["document_uri"] = document_uri
    return error(
        ErrorCode.NOT_READY,
        retry=RetryMetadata(retryable=True),
        details=details,
        workspace=request.workspace,
        adapter=request.adapter,
        generations=request.generations,
    )


def _location_data(mapper: PositionMapper | None, location: Location) -> dict[str, dict[str, int]]:
    if mapper is None:
        raise PositionError("verified reference mapper is unavailable")
    renderer = PublicPositionRenderer(mapper)
    return renderer.range(
        LspPosition(location.range.start.line, location.range.start.character),
        LspPosition(location.range.end.line, location.range.end.character),
    )


def _raw_location_data(location: Location, encoding: PositionEncoding) -> dict[str, object]:
    return raw_lsp_range(
        LspPosition(location.range.start.line, location.range.start.character),
        LspPosition(location.range.end.line, location.range.end.character),
        encoding,
    )


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
