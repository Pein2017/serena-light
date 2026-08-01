"""Compact, deterministic presentation for semantic navigation success.

This module owns presentation only.  Callers retain workspace authorization,
semantic dispatch, snapshot freshness, and rich error conversion.  The compact
DTOs accept already-authorized atomic results and deliberately cannot represent
adapter phases, generations, URIs, query echoes, or compatibility offsets.
"""

from __future__ import annotations

import json
import math
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast

from mcp import types

from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    GenerationMetadata,
    WorkspaceMetadata,
    error,
)

DEFAULT_MAX_ANSWER_CHARS = 12_000
MIN_MAX_ANSWER_CHARS = 512
MAX_MAX_ANSWER_CHARS = 50_000
DEFAULT_MAX_MATCHES = 20
MIN_MAX_MATCHES = 1
MAX_MAX_MATCHES = 100

type CompactPosition = tuple[int, int]
type CompactRange = tuple[CompactPosition, CompactPosition]
type RecordKey = Literal["symbols", "references", "targets"]
type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

_SYMBOL_KINDS: tuple[str, ...] = (
    "file",
    "module",
    "namespace",
    "package",
    "class",
    "method",
    "property",
    "field",
    "constructor",
    "enum",
    "interface",
    "function",
    "variable",
    "constant",
    "string",
    "number",
    "boolean",
    "array",
    "object",
    "key",
    "null",
    "enum_member",
    "struct",
    "event",
    "operator",
    "type_parameter",
)
KNOWN_SYMBOL_KINDS = frozenset(_SYMBOL_KINDS)
_UNKNOWN_KIND = re.compile(r"unknown:-?\d+\Z")
_LANGUAGE_SUFFIXES: Mapping[str, frozenset[str]] = {
    "python": frozenset({".py", ".pyi", ".pyw"}),
    "typescript": frozenset({".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}),
}


def validate_max_answer_chars(value: object) -> int:
    """Return a valid public answer budget or fail before semantic dispatch."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_answer_chars must be an integer from 512 through 50000")
    if not MIN_MAX_ANSWER_CHARS <= value <= MAX_MAX_ANSWER_CHARS:
        raise ValueError("max_answer_chars must be from 512 through 50000")
    return value


def validate_max_matches(value: object = DEFAULT_MAX_MATCHES) -> int:
    """Return a valid public match limit or fail before semantic dispatch."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_matches must be an integer from 1 through 100")
    if not MIN_MAX_MATCHES <= value <= MAX_MAX_MATCHES:
        raise ValueError("max_matches must be from 1 through 100")
    return value


def symbol_kind(value: int) -> str:
    """Render one LSP ``SymbolKind`` with a stable unknown fallback."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("symbol kind must be an integer")
    if 1 <= value <= len(_SYMBOL_KINDS):
        return _SYMBOL_KINDS[value - 1]
    return f"unknown:{value}"


def validate_overview_kind_filters(
    include_kinds: Sequence[str] | None,
    exclude_kinds: Sequence[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate and canonicalize public overview filters before dispatch."""

    return _validate_kind_filter(include_kinds, "include_kinds"), _validate_kind_filter(exclude_kinds, "exclude_kinds")


def compact_range(public_range: Mapping[str, object]) -> CompactRange:
    """Drop compatibility offsets from one current public decoded-text range."""

    start = _compact_position(public_range.get("start"), "start")
    end = _compact_position(public_range.get("end"), "end")
    if end < start:
        raise ValueError("range end precedes its start")
    return start, end


def compact_raw_lsp_range(raw_range: Mapping[str, object]) -> tuple[CompactRange, str]:
    """Preserve an explicitly raw external LSP range without relabelling it decoded-text."""

    basis = raw_range.get("basis")
    if not isinstance(basis, str) or not basis.startswith("lsp_zero_based_line_"):
        raise ValueError("raw LSP range requires an explicit zero-based basis")
    start = _compact_raw_position(raw_range.get("start"), "start")
    end = _compact_raw_position(raw_range.get("end"), "end")
    if end < start:
        raise ValueError("raw LSP range end precedes its start")
    return (start, end), basis


@dataclass(frozen=True, slots=True)
class CompactOverviewSymbol:
    """One symbol-overview node in language-server sibling order."""

    name: str
    kind: int
    children: tuple[CompactOverviewSymbol, ...] = ()
    intrinsic_match: bool = True

    def __post_init__(self) -> None:
        _non_empty(self.name, "overview symbol name")
        symbol_kind(self.kind)
        if any(not isinstance(child, CompactOverviewSymbol) for child in self.children):
            raise TypeError("overview children must be CompactOverviewSymbol values")
        if not isinstance(self.intrinsic_match, bool):
            raise TypeError("overview intrinsic_match must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"name": self.name, "kind": symbol_kind(self.kind)}
        if self.children:
            value["children"] = [child.to_dict() for child in self.children]
        return value

    @property
    def node_count(self) -> int:
        return 1 + sum(child.node_count for child in self.children)


@dataclass(frozen=True, slots=True)
class CompactSymbolMatch:
    """One atomic ``find_symbol`` result."""

    name_path: str
    kind: int
    range: CompactRange
    body: str | None = None
    info: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.name_path, "symbol name_path")
        symbol_kind(self.kind)
        _validate_compact_range(self.range)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name_path": self.name_path,
            "kind": symbol_kind(self.kind),
            "range": _range_json(self.range),
        }
        if self.body is not None:
            value["body"] = self.body
        if self.info is not None:
            value["info"] = self.info
        return value


@dataclass(frozen=True, slots=True)
class CompactReference:
    """One atomic semantic reference and optional containing symbol/snippet."""

    range: CompactRange | None
    symbol: str | None = None
    snippet: str | None = None
    raw_range: CompactRange | None = None
    position_basis: str | None = None

    def __post_init__(self) -> None:
        _validate_coordinate_choice(self.range, self.raw_range, self.position_basis)
        if self.symbol is not None:
            _non_empty(self.symbol, "reference symbol")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.range is not None:
            value["range"] = _range_json(self.range)
        else:
            assert self.raw_range is not None and self.position_basis is not None
            value["raw_range"] = _range_json(self.raw_range)
            value["position_basis"] = self.position_basis
        if self.symbol is not None:
            value["symbol"] = self.symbol
        if self.snippet is not None:
            value["snippet"] = self.snippet
        return value


@dataclass(frozen=True, slots=True)
class CompactTarget:
    """One atomic declaration or implementation target."""

    range: CompactRange | None
    name_path: str | None = None
    kind: int | None = None
    body: str | None = None
    info: str | None = None
    raw_range: CompactRange | None = None
    position_basis: str | None = None

    def __post_init__(self) -> None:
        _validate_coordinate_choice(self.range, self.raw_range, self.position_basis)
        if self.name_path is not None:
            _non_empty(self.name_path, "target name_path")
        if self.kind is not None:
            symbol_kind(self.kind)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.range is not None:
            value["range"] = _range_json(self.range)
        else:
            assert self.raw_range is not None and self.position_basis is not None
            value["raw_range"] = _range_json(self.raw_range)
            value["position_basis"] = self.position_basis
        if self.name_path is not None:
            value["name_path"] = self.name_path
        if self.kind is not None:
            value["kind"] = symbol_kind(self.kind)
        if self.body is not None:
            value["body"] = self.body
        if self.info is not None:
            value["info"] = self.info
        return value


type CompactFlatRecord = CompactSymbolMatch | CompactReference | CompactTarget
type CompactRecord = CompactOverviewSymbol | CompactFlatRecord


@dataclass(frozen=True, slots=True)
class LocatedCompactRecord:
    """File identity paired with one already-authorized flat result."""

    path: str
    record: CompactFlatRecord
    language: str | None = None
    read_only: bool | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_path(self.path))
        if self.language is not None:
            _non_empty(self.language, "language")
        if self.read_only is not None and not isinstance(self.read_only, bool):
            raise TypeError("read_only must be a boolean")
        if self.sha256 is not None and not _valid_sha256(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if self.read_only is False:
            object.__setattr__(self, "read_only", None)
        if not _record_has_body(self.record):
            object.__setattr__(self, "sha256", None)


@dataclass(frozen=True, slots=True)
class CompactFile:
    """One compact file group with exactly one tool-specific records array."""

    path: str
    record_key: RecordKey
    records: tuple[CompactRecord, ...]
    language: str | None = None
    read_only: bool | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_path(self.path))
        if not self.records:
            raise ValueError("compact file groups must not be empty")
        expected = {
            "symbols": (CompactOverviewSymbol, CompactSymbolMatch),
            "references": (CompactReference,),
            "targets": (CompactTarget,),
        }[self.record_key]
        if any(not isinstance(record, expected) for record in self.records):
            raise TypeError(f"{self.record_key} file contains the wrong record type")
        if self.sha256 is not None and not _valid_sha256(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"path": self.path}
        if self.language is not None:
            value["language"] = self.language
        if self.read_only:
            value["read_only"] = True
        if self.sha256 is not None:
            value["sha256"] = self.sha256
        value[self.record_key] = [record.to_dict() for record in self.records]
        return value


@dataclass(frozen=True, slots=True)
class CompactNavigationSuccess:
    """The complete navigation-only success DTO."""

    workspace: str
    files: tuple[CompactFile, ...] = ()
    omitted: int = 0
    coverage: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _non_empty(self.workspace, "workspace")
        if isinstance(self.omitted, bool) or not isinstance(self.omitted, int) or self.omitted < 0:
            raise ValueError("omitted must be a non-negative integer")
        if any(not isinstance(file, CompactFile) for file in self.files):
            raise TypeError("files must contain CompactFile values")
        if self.coverage is not None:
            if any(file.record_key != "references" for file in self.files):
                raise ValueError("coverage is valid only for reference results")
            copied = _copy_json(self.coverage, "coverage")
            if not isinstance(copied, Mapping):  # pragma: no cover - input annotation guard
                raise TypeError("coverage must be a mapping")
            object.__setattr__(self, "coverage", copied)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "workspace": self.workspace,
            "files": [file.to_dict() for file in self.files],
        }
        if self.coverage is not None:
            data["coverage"] = _plain_json(cast(JsonValue, self.coverage))
        data["omitted"] = self.omitted
        return {"ok": True, "data": data}


def ordered_records(records: Sequence[LocatedCompactRecord]) -> tuple[LocatedCompactRecord, ...]:
    """Return exact-deduplicated stable records for pre-group match limiting."""

    return _unique_sorted_records(records)


def group_records(records: Sequence[LocatedCompactRecord]) -> tuple[CompactFile, ...]:
    """Exact-deduplicate, sort, and group flat results deterministically."""

    ordered = _unique_sorted_records(records)
    if not ordered:
        return ()
    record_key = _record_key(ordered[0].record)
    if any(_record_key(item.record) != record_key for item in ordered):
        raise ValueError("one compact response cannot mix tool-specific record types")
    languages = {language for item in ordered if (language := item.language or _infer_language(item.path)) is not None}
    multiple_languages = len(languages) > 1
    groups: list[CompactFile] = []
    for path in dict.fromkeys(item.path for item in ordered):
        items = [item for item in ordered if item.path == path]
        language = cast(str | None, _one_file_value(items, "language")) or _infer_language(path)
        read_only = _one_file_value(items, "read_only")
        hashes = {item.sha256 for item in items if item.sha256 is not None}
        if len(hashes) > 1:
            raise ValueError(f"file group {path!r} has conflicting sha256 values")
        has_body = any(_record_has_body(item.record) for item in items)
        sha256 = next(iter(hashes), None) if has_body else None
        if has_body and sha256 is None:
            raise ValueError(f"body-bearing file group {path!r} requires sha256")
        displayed_language = (
            language if language is not None and (multiple_languages or _infer_language(path) != language) else None
        )
        groups.append(
            CompactFile(
                path=path,
                record_key=record_key,
                records=tuple(item.record for item in items),
                language=displayed_language,
                read_only=cast(bool | None, read_only),
                sha256=sha256,
            )
        )
    return tuple(groups)


def canonical_json(value: Mapping[str, Any] | CompactNavigationSuccess) -> str:
    """Serialize canonical UTF-8-preserving JSON with no insignificant space."""

    payload = value.to_dict() if isinstance(value, CompactNavigationSuccess) else dict(value)
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def render_payload(payload: Mapping[str, Any]) -> types.CallToolResult:
    """Build one explicit MCP result from a complete client-visible JSON value."""

    value = dict(payload)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=canonical_json(value))],
        structuredContent=value,
        isError=False,
    )


def render_success(value: CompactNavigationSuccess) -> types.CallToolResult:
    """Build one explicit MCP result whose text and structured value agree."""

    return render_payload(value.to_dict())


def render_bounded_records(
    workspace: str,
    records: Sequence[LocatedCompactRecord],
    *,
    max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
    omitted: int = 0,
    coverage: Mapping[str, Any] | None = None,
    error_workspace: WorkspaceMetadata | None = None,
    error_adapter: AdapterMetadata | None = None,
    error_generations: GenerationMetadata | None = None,
    error_authorities: Sequence[Mapping[str, Any]] = (),
) -> types.CallToolResult:
    """Render a bounded stable prefix, removing trailing whole records only."""

    budget = validate_max_answer_chars(max_answer_chars)
    if omitted < 0:
        raise ValueError("omitted must be non-negative")
    ordered = list(_unique_sorted_records(records))
    retained = list(ordered)
    while True:
        if ordered and not retained:
            minimum = _minimum_flat_success_chars(workspace, ordered, omitted, coverage)
            return minimum_required_chars_result(
                minimum,
                workspace=error_workspace,
                adapter=error_adapter,
                generations=error_generations,
                authorities=error_authorities,
            )
        pruned = len(ordered) - len(retained)
        success = CompactNavigationSuccess(
            workspace,
            group_records(retained),
            omitted + pruned,
            coverage,
        )
        rendered = render_success(success)
        if _text_length(rendered) <= budget:
            return rendered
        if not retained:
            return minimum_required_chars_result(
                len(canonical_json(success)),
                workspace=error_workspace,
                adapter=error_adapter,
                generations=error_generations,
                authorities=error_authorities,
            )
        retained.pop()


def render_bounded_overview(
    workspace: str,
    files: Sequence[CompactFile],
    *,
    max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
    omitted: int = 0,
    error_workspace: WorkspaceMetadata | None = None,
    error_adapter: AdapterMetadata | None = None,
    error_generations: GenerationMetadata | None = None,
    error_authorities: Sequence[Mapping[str, Any]] = (),
) -> types.CallToolResult:
    """Prune trailing overview nodes without ever orphaning a child."""

    budget = validate_max_answer_chars(max_answer_chars)
    if omitted < 0:
        raise ValueError("omitted must be non-negative")
    original = _validated_overview_files(files)
    retained = original
    removed = 0
    while True:
        if original and not retained:
            return minimum_required_chars_result(
                _minimum_overview_success_chars(workspace, original, omitted),
                workspace=error_workspace,
                adapter=error_adapter,
                generations=error_generations,
                authorities=error_authorities,
            )
        success = CompactNavigationSuccess(workspace, retained, omitted + removed)
        rendered = render_success(success)
        if _text_length(rendered) <= budget:
            return rendered
        retained, removed_now = _remove_last_overview_node(retained)
        if removed_now == 0:
            return minimum_required_chars_result(
                len(canonical_json(success)),
                workspace=error_workspace,
                adapter=error_adapter,
                generations=error_generations,
                authorities=error_authorities,
            )
        removed += removed_now


def minimum_required_chars_result(
    minimum_required_chars: int,
    *,
    workspace: WorkspaceMetadata | None = None,
    adapter: AdapterMetadata | None = None,
    generations: GenerationMetadata | None = None,
    authorities: Sequence[Mapping[str, Any]] = (),
) -> types.CallToolResult:
    """Return the bounded budget error without changing general error semantics."""

    if isinstance(minimum_required_chars, bool) or not isinstance(minimum_required_chars, int):
        raise TypeError("minimum_required_chars must be an integer")
    if minimum_required_chars < 0:
        raise ValueError("minimum_required_chars must be non-negative")
    details: dict[str, Any] = {
        "field": "max_answer_chars",
        "minimum_required_chars": minimum_required_chars,
    }
    if authorities:
        details["authorities"] = list(authorities)
    envelope = error(
        ErrorCode.INVALID_INPUT,
        details=details,
        workspace=workspace,
        adapter=adapter,
        generations=generations,
    )
    payload = envelope.to_dict()
    return render_payload(payload)


def _validate_kind_filter(value: Sequence[str] | None, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{field} must be a sequence of stable symbol-kind strings")
    result: set[str] = set()
    for kind in value:
        if not isinstance(kind, str) or not _valid_kind_label(kind):
            raise ValueError(f"{field} contains an unknown symbol kind")
        result.add(kind)
    return tuple(sorted(result))


def _valid_kind_label(value: str) -> bool:
    if value in KNOWN_SYMBOL_KINDS:
        return True
    if _UNKNOWN_KIND.fullmatch(value) is None:
        return False
    return symbol_kind(int(value.removeprefix("unknown:"))) == value


def _copy_json(value: Any, field: str) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must not contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field} mapping keys must be strings")
            copied[key] = _copy_json(nested, f"{field}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_copy_json(nested, field) for nested in value)
    raise TypeError(f"{field} must contain only JSON values")


def _plain_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(cast(JsonValue, nested)) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(nested) for nested in value]
    return value


def _compact_position(raw: object, field: str) -> CompactPosition:
    if not isinstance(raw, Mapping):
        raise ValueError(f"public range {field} must be an object")
    mapping = cast(Mapping[str, object], raw)
    line = mapping.get("line")
    column = mapping.get("column")
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or isinstance(column, bool)
        or not isinstance(column, int)
        or line < 0
        or column < 0
    ):
        raise ValueError(f"public range {field} requires non-negative integer line and column")
    return line, column


def _compact_raw_position(raw: object, field: str) -> CompactPosition:
    if not isinstance(raw, Mapping):
        raise ValueError(f"raw LSP range {field} must be an object")
    value = cast(Mapping[str, object], raw)
    line = value.get("line")
    character = value.get("character")
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or isinstance(character, bool)
        or not isinstance(character, int)
        or line < 0
        or character < 0
    ):
        raise ValueError(f"raw LSP range {field} requires non-negative integer line and character")
    return line, character


def _validate_coordinate_choice(
    decoded_range: CompactRange | None,
    raw_range: CompactRange | None,
    position_basis: str | None,
) -> None:
    if (decoded_range is None) == (raw_range is None):
        raise ValueError("exactly one decoded or raw range is required")
    selected = decoded_range if decoded_range is not None else raw_range
    assert selected is not None
    _validate_compact_range(selected)
    if raw_range is None and position_basis is not None:
        raise ValueError("decoded ranges must not carry a raw position basis")
    if raw_range is not None and (
        not isinstance(position_basis, str) or not position_basis.startswith("lsp_zero_based_line_")
    ):
        raise ValueError("raw ranges require an explicit LSP position basis")


def _validate_compact_range(value: CompactRange) -> None:
    try:
        start, end = value
        start_line, start_column = start
        end_line, end_column = end
    except (TypeError, ValueError) as exc:
        raise ValueError("compact range must contain two line/column pairs") from exc
    coordinates = (start_line, start_column, end_line, end_column)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in coordinates):
        raise ValueError("compact range coordinates must be non-negative integers")
    if end < start:
        raise ValueError("compact range end precedes its start")


def _range_json(value: CompactRange) -> list[list[int]]:
    return [[value[0][0], value[0][1]], [value[1][0], value[1][1]]]


def _normalize_path(value: str) -> str:
    _non_empty(value, "path")
    normalized = posixpath.normpath(value)
    if value.startswith("/"):
        normalized = f"/{normalized.lstrip('/')}"
    if normalized == ".":
        raise ValueError("path must identify a file")
    return normalized


def _non_empty(value: object, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _record_key(record: CompactFlatRecord) -> RecordKey:
    if isinstance(record, CompactSymbolMatch):
        return "symbols"
    if isinstance(record, CompactReference):
        return "references"
    return "targets"


def _record_has_body(record: CompactFlatRecord) -> bool:
    return isinstance(record, CompactSymbolMatch | CompactTarget) and record.body is not None


def _unique_sorted_records(records: Sequence[LocatedCompactRecord]) -> tuple[LocatedCompactRecord, ...]:
    copied = tuple(records)
    if any(not isinstance(item, LocatedCompactRecord) for item in copied):
        raise TypeError("records must contain LocatedCompactRecord values")
    ordered = sorted(copied, key=_record_order_key)
    seen: set[str] = set()
    result: list[LocatedCompactRecord] = []
    for item in ordered:
        identity = canonical_json(_located_record_dict(item))
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return tuple(result)


def _record_order_key(item: LocatedCompactRecord) -> tuple[Any, ...]:
    record = item.record
    selected_range = record.range
    if selected_range is None:
        assert isinstance(record, CompactReference | CompactTarget)
        selected_range = record.raw_range
    assert selected_range is not None
    start_line, start_column = selected_range[0]
    end_line, end_column = selected_range[1]
    name = ""
    kind = ""
    if isinstance(record, CompactSymbolMatch):
        name = record.name_path
        kind = symbol_kind(record.kind)
    elif isinstance(record, CompactReference):
        name = record.symbol or ""
    else:
        name = record.name_path or ""
        kind = symbol_kind(record.kind) if record.kind is not None else ""
    return (
        item.path,
        start_line,
        start_column,
        end_line,
        end_column,
        name,
        kind,
        canonical_json(_located_record_dict(item)),
    )


def _located_record_dict(item: LocatedCompactRecord) -> dict[str, Any]:
    return {
        "path": item.path,
        "language": item.language,
        "read_only": item.read_only,
        "sha256": item.sha256,
        "record": item.record.to_dict(),
    }


def _one_file_value(items: Sequence[LocatedCompactRecord], field: str) -> object:
    values = {getattr(item, field) for item in items if getattr(item, field) is not None}
    if len(values) > 1:
        raise ValueError(f"file group {items[0].path!r} has conflicting {field} values")
    return next(iter(values), None)


def _infer_language(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    for language, suffixes in _LANGUAGE_SUFFIXES.items():
        if suffix in suffixes:
            return language
    return None


def _text_length(result: types.CallToolResult) -> int:
    block = result.content[0]
    if not isinstance(block, types.TextContent):  # pragma: no cover - construction invariant
        raise TypeError("compact renderer produced a non-text content block")
    return len(block.text)


def _minimum_flat_success_chars(
    workspace: str,
    records: Sequence[LocatedCompactRecord],
    omitted: int,
    coverage: Mapping[str, Any] | None,
) -> int:
    total = len(records)
    first_prefix = CompactNavigationSuccess(
        workspace,
        group_records((records[0],)),
        omitted + total - 1,
        coverage,
    )
    return len(canonical_json(first_prefix))


def _validated_overview_files(files: Sequence[CompactFile]) -> tuple[CompactFile, ...]:
    ordered = tuple(sorted(files, key=lambda file: file.path))
    for file in ordered:
        wrong_record = any(not isinstance(record, CompactOverviewSymbol) for record in file.records)
        if file.record_key != "symbols" or wrong_record:
            raise TypeError("overview rendering requires overview-symbol file groups")
        if file.sha256 is not None:
            raise ValueError("overview file groups must not contain sha256")
    return ordered


def _remove_last_overview_node(files: tuple[CompactFile, ...]) -> tuple[tuple[CompactFile, ...], int]:
    mutable = list(files)
    for file_index in range(len(mutable) - 1, -1, -1):
        symbols = cast(tuple[CompactOverviewSymbol, ...], mutable[file_index].records)
        if not symbols:
            continue
        retained, removed = _remove_last_symbol(symbols)
        if retained:
            mutable[file_index] = replace(mutable[file_index], records=retained)
        else:
            del mutable[file_index]
        return tuple(mutable), removed
    return files, 0


def _remove_last_symbol(
    symbols: tuple[CompactOverviewSymbol, ...],
) -> tuple[tuple[CompactOverviewSymbol, ...], int]:
    last = symbols[-1]
    if last.children:
        retained_children, removed = _remove_last_symbol(last.children)
        if not retained_children and not last.intrinsic_match:
            return symbols[:-1], removed + 1
        return (*symbols[:-1], replace(last, children=retained_children)), removed
    return symbols[:-1], 1


def _minimum_overview_success_chars(
    workspace: str,
    files: Sequence[CompactFile],
    omitted: int,
) -> int:
    total = sum(cast(CompactOverviewSymbol, record).node_count for file in files for record in file.records)
    first_file = files[0]
    first_symbol = cast(CompactOverviewSymbol, first_file.records[0])
    prefix, retained = _minimum_overview_symbol_prefix(first_symbol)
    single = replace(first_file, records=(prefix,))
    value = CompactNavigationSuccess(workspace, (single,), omitted + total - retained)
    return len(canonical_json(value))


def _minimum_overview_symbol_prefix(
    symbol: CompactOverviewSymbol,
) -> tuple[CompactOverviewSymbol, int]:
    """Return the smallest valid prefix rooted at the first retained symbol."""

    if symbol.intrinsic_match:
        return replace(symbol, children=()), 1
    if not symbol.children:  # pragma: no cover - filtering construction invariant
        raise ValueError("structural overview ancestor must retain a matching descendant")
    child, retained = _minimum_overview_symbol_prefix(symbol.children[0])
    return replace(symbol, children=(child,)), retained + 1


__all__ = (
    "DEFAULT_MAX_ANSWER_CHARS",
    "DEFAULT_MAX_MATCHES",
    "KNOWN_SYMBOL_KINDS",
    "MAX_MAX_ANSWER_CHARS",
    "MAX_MAX_MATCHES",
    "MIN_MAX_ANSWER_CHARS",
    "MIN_MAX_MATCHES",
    "CompactFile",
    "CompactNavigationSuccess",
    "CompactOverviewSymbol",
    "CompactRange",
    "CompactReference",
    "CompactSymbolMatch",
    "CompactTarget",
    "LocatedCompactRecord",
    "canonical_json",
    "compact_range",
    "compact_raw_lsp_range",
    "group_records",
    "minimum_required_chars_result",
    "ordered_records",
    "render_bounded_overview",
    "render_bounded_records",
    "render_success",
    "symbol_kind",
    "validate_max_answer_chars",
    "validate_max_matches",
    "validate_overview_kind_filters",
)
