"""Transport-neutral definition and implementation navigation cores.

The public Serena-compatible ``find_declaration`` name deliberately dispatches
LSP ``textDocument/definition``.  This module owns occurrence and source-symbol
selection, capability gates, bounds, and stable envelopes; an injected adapter
seam owns authorization/readiness, wire transport, and normalization plus trust
classification of returned locations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from serena_light.lsp.adapter import DerivedToolAvailability, RawLspProviders
from serena_light.lsp.normalize import NormalizedSymbol, Position, Range
from serena_light.lsp.positions import (
    FileSnapshot,
    LspPosition,
    PositionEncoding,
    PositionError,
    PublicPositionRenderer,
    raw_lsp_range,
)
from serena_light.tools.envelopes import (
    ErrorCode,
    ErrorEnvelope,
    JsonValue,
    ToolEnvelope,
    TruncationMetadata,
    error,
    success,
)
from serena_light.tools.navigation import DocumentNavigation, DocumentSymbolInput

DEFINITION_METHOD = "textDocument/definition"
IMPLEMENTATION_METHOD = "textDocument/implementation"
_REGEX_FLAGS = re.MULTILINE | re.DOTALL
_MIN_SYMBOL_KIND = 1
_MAX_SYMBOL_KIND = 26

type RawLocations = object
type NormalizedLocation = Mapping[str, Any] | ClassifiedLocationInput


@dataclass(frozen=True, slots=True)
class ClassifiedLocationInput:
    """One classified adapter location before public coordinate rendering.

    A verified location carries the exact immutable target snapshot used for
    classification.  When no authorized snapshot is available, the same raw
    LSP range may be retained, but it is emitted only under an explicitly
    named raw coordinate basis.  ``metadata`` must never contain coordinates;
    this core is their single public renderer.
    """

    metadata: Mapping[str, Any]
    lsp_range: Range | None = None
    snapshot: FileSnapshot | None = None
    position_encoding: PositionEncoding = PositionEncoding.UTF16
    semantic_info: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        forbidden = {"range", "raw_lsp_range", "body", "info"}.intersection(self.metadata)
        if forbidden:
            raise ValueError(f"classified location metadata contains rendered fields: {sorted(forbidden)!r}")
        if self.snapshot is not None and self.lsp_range is None:
            raise ValueError("a classified location snapshot requires an LSP range")
        if self.semantic_info is not None and {
            "range",
            "raw_lsp_range",
            "selection_range",
            "selectionRange",
            "body",
        }.intersection(self.semantic_info):
            raise ValueError("classified location semantic info contains a rendered or raw field")

    @classmethod
    def verified(
        cls,
        metadata: Mapping[str, Any],
        lsp_range: Range,
        snapshot: FileSnapshot,
        position_encoding: PositionEncoding = PositionEncoding.UTF16,
        *,
        semantic_info: Mapping[str, Any] | None = None,
    ) -> ClassifiedLocationInput:
        """Build a location backed by the adapter's exact verified snapshot."""

        return cls(metadata, lsp_range, snapshot, position_encoding, semantic_info)

    @classmethod
    def raw_lsp(
        cls,
        metadata: Mapping[str, Any],
        lsp_range: Range,
        position_encoding: PositionEncoding = PositionEncoding.UTF16,
    ) -> ClassifiedLocationInput:
        """Build an explicitly raw LSP-basis location without a snapshot."""

        return cls(metadata, lsp_range, None, position_encoding)


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    """Raw LSP providers and the distinct Serena-compatible derived tools."""

    raw: RawLspProviders
    derived: DerivedToolAvailability

    @classmethod
    def from_raw(cls, raw: RawLspProviders) -> CapabilityMatrix:
        return cls(raw=raw, derived=DerivedToolAvailability.from_raw(raw))

    def to_dict(self) -> dict[str, dict[str, bool]]:
        return {
            "raw": {
                "definitionProvider": self.raw.definition,
                "declarationProvider": self.raw.declaration,
                "implementationProvider": self.raw.implementation,
                "referencesProvider": self.raw.references,
                "documentSymbolProvider": self.raw.document_symbols,
                "workspaceSymbolProvider": self.raw.workspace_symbols,
            },
            "derived": {
                "find_declaration": self.derived.find_declaration,
                "find_implementations": self.derived.find_implementations,
                "find_referencing_symbols": self.derived.find_referencing_symbols,
                "get_symbols_overview": self.derived.get_symbols_overview,
                "global_find_symbol": self.derived.global_find_symbol,
            },
        }


@dataclass(frozen=True, slots=True)
class SemanticDocumentInput:
    """One authorized, ready document paired with its current capabilities."""

    document: DocumentSymbolInput
    capabilities: CapabilityMatrix


class DeclarationAdapterSeam(Protocol):
    """Injected adapter boundary for definition and implementation semantics.

    ``normalize_and_classify_locations`` must resolve Location/LocationLink
    shapes, enrich requested body/info fields, and classify every path under the
    active-workspace or allowed-read-only-external trust policy.  It may return
    a typed envelope when classification rejects a result.
    """

    def load_semantic_document(self, relative_path: str) -> SemanticDocumentInput | ErrorEnvelope: ...

    def request_locations(
        self,
        method: str,
        *,
        document_uri: str,
        position: LspPosition,
        capture_target_symbols: bool = False,
    ) -> RawLocations: ...

    def normalize_and_classify_locations(
        self,
        raw_locations: RawLocations,
        *,
        include_body: bool,
        include_info: bool,
    ) -> Sequence[NormalizedLocation] | ErrorEnvelope: ...


class DeclarationNavigationService:
    """Serena-compatible declaration and implementation semantic cores."""

    def __init__(self, adapter: DeclarationAdapterSeam) -> None:
        self._adapter = adapter

    def find_declaration(
        self,
        relative_path: str,
        regex: str,
        containing_symbol_name_path: str | None = None,
        include_body: bool = False,
        include_info: bool = False,
    ) -> ToolEnvelope:
        """Resolve the one captured occurrence through LSP definition only."""

        if not _valid_relative_path(relative_path):
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "relative_path", "reason": "expected_normalized_relative_file_path"},
            )
        pattern, locator_error = _compile_locator(regex)
        if pattern is None:
            return error(ErrorCode.INVALID_INPUT, details={"field": "regex", "reason": locator_error})
        if containing_symbol_name_path is not None and not _valid_name_path(containing_symbol_name_path):
            return error(ErrorCode.INVALID_INPUT, details={"field": "containing_symbol_name_path"})

        loaded = self._load(relative_path)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        document, capabilities = loaded
        if not capabilities.raw.definition:
            return _unsupported(document, capabilities, "find_declaration", DEFINITION_METHOD)

        search_start = 0
        search_end = len(document.snapshot.text)
        if containing_symbol_name_path is not None:
            container = _resolve_symbol(document, containing_symbol_name_path)
            if isinstance(container, ErrorEnvelope):
                return container
            try:
                search_start = document.mapper.lsp_to_text_offset(_lsp_position(container.location.range.start))
                search_end = document.mapper.lsp_to_text_offset(_lsp_position(container.location.range.end))
            except PositionError:
                return _invalid_adapter_result(document, "containing_symbol_name_path")

        selected_text = document.snapshot.text[search_start:search_end]
        capture_offsets: list[int] = []
        for match in pattern.finditer(selected_text):
            capture_start = match.start(1)
            if capture_start < 0:
                return _invalid(document, "regex", reason="capture_group_did_not_participate")
            capture_offsets.append(search_start + capture_start)
        if not capture_offsets:
            return _missing(document, {"relative_path": relative_path, "regex": regex})
        if len(capture_offsets) > 1:
            return _ambiguous(
                document,
                {
                    "relative_path": relative_path,
                    "regex": regex,
                    "occurrence_count": len(capture_offsets),
                },
            )
        try:
            position = document.mapper.text_offset_to_lsp(capture_offsets[0])
        except PositionError:
            return _invalid(document, "regex", reason="capture_is_not_an_lsp_position")

        raw_locations = self._adapter.request_locations(
            DEFINITION_METHOD,
            document_uri=document.uri,
            position=position,
            capture_target_symbols=include_info,
        )
        locations = self._normalize(
            document,
            raw_locations,
            include_body=include_body,
            include_info=include_info,
        )
        if isinstance(locations, ErrorEnvelope):
            return locations
        if not locations:
            return _missing(
                document,
                {"relative_path": relative_path, "regex": regex, "method": DEFINITION_METHOD},
            )
        result_data = {
            "relative_path": relative_path,
            "capabilities": capabilities.to_dict(),
            "locations": locations,
        }
        return success(
            cast(JsonValue, result_data),
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )

    def find_implementations(
        self,
        name_path: str,
        relative_path: str,
        include_info: bool = False,
        include_kinds: Sequence[int] | None = None,
        exclude_kinds: Sequence[int] | None = None,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Resolve one source symbol and return bounded implementation locations."""

        included = _parse_kinds(include_kinds)
        excluded = _parse_kinds(exclude_kinds)
        if (
            not _valid_relative_path(relative_path)
            or not _valid_name_path(name_path)
            or included is None
            or excluded is None
            or isinstance(max_answer_chars, bool)
            or not isinstance(max_answer_chars, int)
            or max_answer_chars <= 0
        ):
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "name_path, relative_path, include_kinds, exclude_kinds, or max_answer_chars"},
            )

        loaded = self._load(relative_path)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        document, capabilities = loaded
        if not capabilities.raw.implementation:
            return _unsupported(document, capabilities, "find_implementations", IMPLEMENTATION_METHOD)

        source_symbol = _resolve_symbol(document, name_path)
        if isinstance(source_symbol, ErrorEnvelope):
            return source_symbol
        raw_locations = self._adapter.request_locations(
            IMPLEMENTATION_METHOD,
            document_uri=document.uri,
            position=_lsp_position(source_symbol.selection_range.start),
            capture_target_symbols=include_info or bool(included) or bool(excluded),
        )
        normalized = self._normalize(
            document,
            raw_locations,
            include_body=False,
            include_info=include_info,
        )
        if isinstance(normalized, ErrorEnvelope):
            return normalized

        filtered: list[dict[str, Any]] = []
        kind_omitted = 0
        for location in normalized:
            if "kind" not in location:
                # Location and LocationLink responses do not carry SymbolKind.
                # An include filter requires positive evidence; an exclude
                # filter cannot reject an unknown kind without inventing one.
                if not included:
                    filtered.append(dict(location))
                else:
                    kind_omitted += 1
                continue
            kind = location.get("kind")
            if isinstance(kind, bool) or not isinstance(kind, int):
                return _invalid_adapter_result(document, "normalized_locations.kind")
            if included and kind not in included:
                kind_omitted += 1
                continue
            if kind in excluded:
                kind_omitted += 1
                continue
            filtered.append(dict(location))
        filtered.sort(key=_canonical_json)

        base = {
            "relative_path": relative_path,
            "name_path": name_path,
            "capabilities": capabilities.to_dict(),
            "locations": [],
        }
        minimum_required = len(_canonical_json(base))
        if minimum_required > max_answer_chars:
            return _invalid(document, "max_answer_chars", minimum_required=minimum_required)
        kept: list[dict[str, Any]] = []
        for location in filtered:
            candidate = {**base, "locations": [*kept, location]}
            if len(_canonical_json(candidate)) > max_answer_chars:
                break
            kept.append(location)
        omitted = kind_omitted + len(filtered) - len(kept)
        result_data = {**base, "locations": kept}
        return success(
            cast(JsonValue, result_data),
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
            truncation=TruncationMetadata(omitted > 0, omitted),
        )

    def _load(self, relative_path: str) -> tuple[DocumentNavigation, CapabilityMatrix] | ErrorEnvelope:
        loaded = self._adapter.load_semantic_document(relative_path)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        if loaded.document.relative_path != relative_path:
            return error(ErrorCode.INVALID_PATH, details={"path": relative_path})
        try:
            return DocumentNavigation.from_input(loaded.document), loaded.capabilities
        except (PositionError, TypeError, ValueError):
            return error(ErrorCode.INVALID_INPUT, details={"path": relative_path})

    def _normalize(
        self,
        document: DocumentNavigation,
        raw_locations: RawLocations,
        *,
        include_body: bool,
        include_info: bool,
    ) -> list[dict[str, Any]] | ErrorEnvelope:
        normalized = self._adapter.normalize_and_classify_locations(
            raw_locations,
            include_body=include_body,
            include_info=include_info,
        )
        if isinstance(normalized, ErrorEnvelope):
            return normalized
        locations: list[dict[str, Any]] = []
        try:
            for location in normalized:
                if isinstance(location, ClassifiedLocationInput):
                    copied = _render_classified_location(
                        location,
                        include_body=include_body,
                        include_info=include_info,
                    )
                    if copied is None:
                        return _unmapped_requested_content(document)
                    _canonical_json(copied)
                    locations.append(copied)
                    continue
                if not isinstance(location, Mapping):
                    raise TypeError
                copied = dict(location)
                if {"range", "raw_lsp_range", "body", "info"}.intersection(copied):
                    raise ValueError("adapter mappings must not contain pre-rendered location fields")
                _canonical_json(copied)
                locations.append(copied)
        except (TypeError, ValueError):
            return _invalid_adapter_result(document, "normalized_locations")
        locations.sort(key=_canonical_json)
        return locations


def _render_classified_location(
    location: ClassifiedLocationInput,
    *,
    include_body: bool,
    include_info: bool,
) -> dict[str, Any] | None:
    data = dict(location.metadata)
    lsp_range = location.lsp_range
    if lsp_range is None:
        return data
    if location.snapshot is None:
        if include_body or include_info:
            return None
        data["raw_lsp_range"] = raw_lsp_range(
            _lsp_position(lsp_range.start),
            _lsp_position(lsp_range.end),
            location.position_encoding,
        )
        return data
    renderer = PublicPositionRenderer.from_snapshot(location.snapshot, location.position_encoding)
    start = _lsp_position(lsp_range.start)
    end = _lsp_position(lsp_range.end)
    rendered_range = renderer.range(start, end)
    data["range"] = rendered_range
    if include_body:
        data["body"] = renderer.text(start, end)
        data["sha256"] = hashlib.sha256(location.snapshot.raw_bytes).hexdigest()
    if include_info and location.semantic_info:
        data["info"] = dict(location.semantic_info)
    return data


def _unmapped_requested_content(document: DocumentNavigation) -> ErrorEnvelope:
    return error(
        ErrorCode.UNSUPPORTED,
        details={
            "operation": "render_semantic_location_content",
            "reason": "verified_target_snapshot_unavailable",
        },
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _compile_locator(value: object) -> tuple[re.Pattern[str] | None, str | None]:
    if not isinstance(value, str):
        return None, "expected_string"
    try:
        pattern = re.compile(value, _REGEX_FLAGS)
    except re.error:
        return None, "invalid_python_regex"
    if pattern.groups == 0:
        return None, "expected_exactly_one_capture_group_got_zero"
    if pattern.groups > 1:
        return None, f"expected_exactly_one_capture_group_got_{pattern.groups}"
    return pattern, None


def _parse_kinds(values: Sequence[int] | None) -> frozenset[int] | None:
    if values is None:
        return frozenset()
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        return None
    parsed: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not _MIN_SYMBOL_KIND <= value <= _MAX_SYMBOL_KIND:
            return None
        parsed.add(value)
    return frozenset(parsed)


def _resolve_symbol(document: DocumentNavigation, name_path: str) -> NormalizedSymbol | ErrorEnvelope:
    absolute = name_path.startswith("/")
    components = tuple(name_path.lstrip("/").rstrip("/").split("/"))
    candidates = [
        symbol
        for root in document.symbols
        for symbol in root.iter_depth_first()
        if _matches_name_path(symbol, components, absolute)
    ]
    candidates.sort(key=_symbol_order_key)
    if not candidates:
        return _missing(document, {"relative_path": document.relative_path, "name_path": name_path})
    if len(candidates) > 1:
        return _ambiguous(
            document,
            {
                "relative_path": document.relative_path,
                "name_path": name_path,
                "candidates": ["/".join(symbol.name_path) for symbol in candidates],
            },
        )
    return candidates[0]


def _matches_name_path(symbol: NormalizedSymbol, components: tuple[str, ...], absolute: bool) -> bool:
    if len(symbol.name_path) < len(components) or (absolute and len(symbol.name_path) != len(components)):
        return False
    return all(
        _segment_matches(actual, expected)
        for actual, expected in zip(symbol.name_path[-len(components) :], components, strict=True)
    )


def _segment_matches(actual: str, expected: str) -> bool:
    """Let an unindexed Serena path expose overload ambiguity explicitly."""

    if actual == expected:
        return True
    prefix = f"{expected}["
    return actual.startswith(prefix) and actual.endswith("]") and actual[len(prefix) : -1].isdigit()


def _unsupported(
    document: DocumentNavigation,
    capabilities: CapabilityMatrix,
    operation: str,
    method: str,
) -> ErrorEnvelope:
    return error(
        ErrorCode.UNSUPPORTED,
        details={"operation": operation, "method": method, "capabilities": capabilities.to_dict()},
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _missing(document: DocumentNavigation, details: Mapping[str, Any]) -> ErrorEnvelope:
    return error(
        ErrorCode.SYMBOL_NOT_FOUND,
        details=details,
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _ambiguous(document: DocumentNavigation, details: Mapping[str, Any]) -> ErrorEnvelope:
    return error(
        ErrorCode.AMBIGUOUS_SYMBOL,
        details=details,
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _invalid(
    document: DocumentNavigation,
    field: str,
    *,
    reason: str | None = None,
    minimum_required: int | None = None,
) -> ErrorEnvelope:
    details: dict[str, Any] = {"field": field}
    if reason is not None:
        details["reason"] = reason
    if minimum_required is not None:
        details["minimum_required"] = minimum_required
    return error(
        ErrorCode.INVALID_INPUT,
        details=details,
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _invalid_adapter_result(document: DocumentNavigation, field: str) -> ErrorEnvelope:
    return _invalid(document, field, reason="adapter_result_is_invalid")


def _valid_relative_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _valid_name_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return all(component for component in value.lstrip("/").rstrip("/").split("/"))


def _symbol_order_key(symbol: NormalizedSymbol) -> tuple[int, int, tuple[str, ...]]:
    start = symbol.location.range.start
    return start.line, start.character, symbol.name_path


def _lsp_position(value: Position) -> LspPosition:
    return LspPosition(value.line, value.character)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
