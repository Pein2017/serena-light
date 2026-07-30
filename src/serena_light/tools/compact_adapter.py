"""Adapt rich internal navigation envelopes to the compact agent-facing schema.

Semantic cores deliberately retain their snapshot, adapter, and generation
evidence.  This module is the single public presentation boundary: it removes
only successful navigation metadata, keeps failures on the rich envelope path,
and hands the complete DTO to the exact MCP renderer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from mcp import types

from serena_light.tools.compact import (
    DEFAULT_MAX_ANSWER_CHARS,
    DEFAULT_MAX_MATCHES,
    CompactFile,
    CompactOverviewSymbol,
    CompactReference,
    CompactSymbolMatch,
    CompactTarget,
    LocatedCompactRecord,
    compact_range,
    compact_raw_lsp_range,
    ordered_records,
    render_bounded_overview,
    render_bounded_records,
    validate_max_answer_chars,
    validate_max_matches,
    validate_overview_kind_filters,
)
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    GenerationMetadata,
    WorkspaceMetadata,
    error,
)

NAVIGATION_OPERATIONS = frozenset(
    {
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
        "find_declaration",
        "find_implementations",
    }
)
_MAX_ERROR_AUTHORITIES = 2


def compact_navigation_result(
    operation: str,
    envelope: Mapping[str, object],
    *,
    max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
    max_matches: int = DEFAULT_MAX_MATCHES,
    include_kinds: Sequence[str] | None = None,
    exclude_kinds: Sequence[str] | None = None,
    include_snippets: bool = False,
) -> types.CallToolResult:
    """Convert one successful rich internal navigation envelope without leaks."""

    authority_workspace: WorkspaceMetadata | None = None
    authority_adapter: AdapterMetadata | None = None
    authority_generations: GenerationMetadata | None = None
    error_authorities: tuple[Mapping[str, Any], ...] = ()
    try:
        budget = validate_max_answer_chars(max_answer_chars)
        match_limit = validate_max_matches(max_matches)
        if operation not in NAVIGATION_OPERATIONS or envelope.get("ok") is not True:
            raise ValueError("compact adapter requires navigation success")
        authority_parse_failed = False
        try:
            authority_workspace = _workspace_metadata(envelope.get("workspace"))
        except (TypeError, ValueError):
            authority_parse_failed = True
        try:
            authority_adapter = _adapter_metadata(envelope.get("adapter"))
        except (TypeError, ValueError):
            authority_parse_failed = True
        try:
            authority_generations = _generation_metadata(envelope.get("generations"))
        except (TypeError, ValueError):
            authority_parse_failed = True
        if authority_parse_failed:
            raise ValueError("compact navigation success contains malformed authority")
        if authority_workspace is None:
            raise ValueError("compact navigation success requires workspace metadata")
        workspace = authority_workspace.root
        data = _mapping(envelope.get("data"), "data")
        if operation == "get_symbols_overview":
            included, excluded = validate_overview_kind_filters(include_kinds, exclude_kinds)
            files, filter_omitted = _overview_files(data, frozenset(included), frozenset(excluded))
            return render_bounded_overview(
                workspace,
                files,
                max_answer_chars=budget,
                omitted=_overview_internal_omitted(data, envelope) + filter_omitted,
                error_workspace=authority_workspace,
                error_adapter=authority_adapter,
                error_generations=authority_generations,
            )

        omitted = _internal_omitted(envelope)
        coverage: Mapping[str, Any] | None = None
        if operation == "find_symbol":
            if authority_adapter is None or authority_generations is None:
                error_authorities = _symbol_authorities(data)
            records = _symbol_records(data, envelope)
            ordered = ordered_records(records)
            omitted += max(0, len(ordered) - match_limit)
            records = ordered[:match_limit]
        elif operation == "find_referencing_symbols":
            records = _reference_records(data, include_snippets=include_snippets)
            raw_coverage = data.get("coverage")
            coverage = _mapping(raw_coverage, "coverage") if raw_coverage is not None else None
        else:
            records = _target_records(data)
        return render_bounded_records(
            workspace,
            records,
            max_answer_chars=budget,
            omitted=omitted,
            coverage=coverage,
            error_workspace=authority_workspace,
            error_adapter=authority_adapter,
            error_generations=authority_generations,
            error_authorities=error_authorities,
        )
    except (KeyError, TypeError, ValueError):
        return _malformed_result(
            operation,
            workspace=authority_workspace,
            adapter=authority_adapter,
            generations=authority_generations,
            authorities=error_authorities,
        )


def _overview_files(
    data: Mapping[str, object],
    included: frozenset[str],
    excluded: frozenset[str],
) -> tuple[tuple[CompactFile, ...], int]:
    path = _string(data.get("relative_path"), "relative_path")
    raw_symbols = _sequence(data.get("symbols"), "symbols")
    symbols: list[CompactOverviewSymbol] = []
    omitted = 0
    for raw in raw_symbols:
        rendered, removed = _overview_symbol(_mapping(raw, "symbol"), included, excluded)
        omitted += removed
        if rendered is not None:
            symbols.append(rendered)
    if not symbols:
        return (), omitted
    return (CompactFile(path, "symbols", tuple(symbols)),), omitted


def _overview_symbol(
    raw: Mapping[str, object],
    included: frozenset[str],
    excluded: frozenset[str],
) -> tuple[CompactOverviewSymbol | None, int]:
    """Filter post-order so retained descendants keep their ancestor path."""

    name = _string(raw.get("name"), "symbol.name")
    kind = _integer(raw.get("kind"), "symbol.kind")
    from serena_light.tools.compact import symbol_kind

    rendered_kind = symbol_kind(kind)
    raw_children = raw.get("children", ())
    children: list[CompactOverviewSymbol] = []
    omitted = 0
    for child in _sequence(raw_children, "symbol.children"):
        rendered, removed = _overview_symbol(_mapping(child, "symbol.child"), included, excluded)
        omitted += removed
        if rendered is not None:
            children.append(rendered)
    matches = (not included or rendered_kind in included) and rendered_kind not in excluded
    if matches or children:
        return CompactOverviewSymbol(name, kind, tuple(children), intrinsic_match=matches), omitted
    return None, omitted + 1


def _symbol_records(
    data: Mapping[str, object],
    envelope: Mapping[str, object],
) -> tuple[LocatedCompactRecord, ...]:
    default_language = _adapter_language(envelope)
    raw_items: list[tuple[Mapping[str, object], str, str | None, str | None]] = []
    if "symbol" in data:
        raw_items.append(
            (
                _mapping(data.get("symbol"), "symbol"),
                _string(data.get("relative_path"), "relative_path"),
                _optional_string(data.get("sha256")),
                default_language,
            )
        )
    else:
        for raw in _sequence(data.get("symbols"), "symbols"):
            item = _mapping(raw, "symbols.item")
            if "symbol" in item:
                raw_items.append(
                    (
                        _mapping(item.get("symbol"), "symbols.item.symbol"),
                        _string(item.get("relative_path"), "symbols.item.relative_path"),
                        _optional_string(item.get("sha256")),
                        default_language,
                    )
                )
            else:
                adapter = item.get("adapter")
                language = _adapter_language(item) if isinstance(adapter, Mapping) else default_language
                raw_items.append(
                    (
                        item,
                        _string(item.get("relative_path"), "symbols.item.relative_path"),
                        _optional_string(item.get("sha256")),
                        language,
                    )
                )

    records: list[LocatedCompactRecord] = []
    for item, path, sha256, language in raw_items:
        location = item.get("location")
        range_value = (
            _mapping(_mapping(location, "symbol.location").get("range"), "symbol.location.range")
            if location is not None
            else _mapping(item.get("range"), "symbol.range")
        )
        body = _optional_string(item.get("body"), allow_empty=True)
        info = _compact_info(item.get("info"))
        record = CompactSymbolMatch(
            _string(item.get("name_path"), "symbol.name_path"),
            _integer(item.get("kind"), "symbol.kind"),
            compact_range(range_value),
            body=body,
            info=info,
        )
        records.append(
            LocatedCompactRecord(
                path,
                record,
                language=language,
                sha256=sha256 if body is not None else None,
            )
        )
    return tuple(records)


def _reference_records(data: Mapping[str, object], *, include_snippets: bool) -> tuple[LocatedCompactRecord, ...]:
    records: list[LocatedCompactRecord] = []
    for raw in _sequence(data.get("references"), "references"):
        item = _mapping(raw, "references.item")
        location = _mapping(item.get("location"), "reference.location")
        decoded, raw_range, basis = _coordinates(location)
        container = _mapping(item.get("container"), "reference.container")
        symbol = _optional_string(container.get("name_path"))
        record = CompactReference(
            decoded,
            symbol=symbol,
            snippet=_optional_string(item.get("snippet"), allow_empty=True) if include_snippets else None,
            raw_range=raw_range,
            position_basis=basis,
        )
        records.append(
            LocatedCompactRecord(
                _string(item.get("path"), "reference.path"),
                record,
                read_only=_optional_bool(item.get("read_only_external")),
            )
        )
    return tuple(records)


def _target_records(data: Mapping[str, object]) -> tuple[LocatedCompactRecord, ...]:
    records: list[LocatedCompactRecord] = []
    for raw in _sequence(data.get("locations"), "locations"):
        item = _mapping(raw, "locations.item")
        path = item.get("relative_path")
        read_only = item.get("read_only_external") is True
        if path is None:
            path = item.get("absolute_path")
        decoded, raw_range, basis = _target_coordinates(item)
        body = _optional_string(item.get("body"), allow_empty=True)
        record = CompactTarget(
            decoded,
            name_path=_optional_string(item.get("name_path")),
            kind=_optional_integer(item.get("kind")),
            body=body,
            info=_compact_info(item.get("info")),
            raw_range=raw_range,
            position_basis=basis,
        )
        records.append(
            LocatedCompactRecord(
                _string(path, "target.path"),
                record,
                read_only=read_only or None,
                sha256=_optional_string(item.get("sha256")) if body is not None else None,
            )
        )
    return tuple(records)


def _coordinates(
    value: Mapping[str, object],
) -> tuple[tuple[tuple[int, int], tuple[int, int]] | None, tuple[tuple[int, int], tuple[int, int]] | None, str | None]:
    if "basis" in value:
        raw_range, basis = compact_raw_lsp_range(value)
        return None, raw_range, basis
    return compact_range(value), None, None


def _target_coordinates(
    value: Mapping[str, object],
) -> tuple[tuple[tuple[int, int], tuple[int, int]] | None, tuple[tuple[int, int], tuple[int, int]] | None, str | None]:
    if "range" in value:
        return compact_range(_mapping(value.get("range"), "target.range")), None, None
    raw_range, basis = compact_raw_lsp_range(_mapping(value.get("raw_lsp_range"), "target.raw_lsp_range"))
    return None, raw_range, basis


def _workspace_metadata(value: object) -> WorkspaceMetadata | None:
    if value is None:
        return None
    workspace = _mapping(value, "workspace")
    return WorkspaceMetadata(
        root=_string(workspace.get("root"), "workspace.root"),
        kind=_string(workspace.get("kind"), "workspace.kind"),
        working_subdirectory=_string(workspace.get("working_subdirectory"), "workspace.working_subdirectory"),
    )


def _adapter_metadata(value: object) -> AdapterMetadata | None:
    if value is None:
        return None
    adapter = _mapping(value, "adapter")
    return AdapterMetadata(
        name=_string(adapter.get("name"), "adapter.name"),
        language=_optional_string(adapter.get("language")),
    )


def _generation_metadata(value: object) -> GenerationMetadata | None:
    if value is None:
        return None
    generations = _mapping(value, "generations")
    return GenerationMetadata(
        trust=_optional_nonnegative_integer(generations.get("trust"), "generations.trust"),
        program=_optional_nonnegative_integer(generations.get("program"), "generations.program"),
        document=_optional_nonnegative_integer(generations.get("document"), "generations.document"),
        index=_optional_nonnegative_integer(generations.get("index"), "generations.index"),
        scope=_optional_string(generations.get("scope")),
    )


def _adapter_language(envelope: Mapping[str, object]) -> str | None:
    adapter = envelope.get("adapter")
    if not isinstance(adapter, Mapping):
        return None
    return _optional_string(cast(Mapping[str, object], adapter).get("language"))


def _symbol_authorities(data: Mapping[str, object]) -> tuple[Mapping[str, Any], ...]:
    """Collect the fixed global providers' item-owned error authority."""

    authorities: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in _sequence(data.get("symbols"), "symbols"):
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        try:
            adapter = _adapter_metadata(item.get("adapter"))
            generations = _generation_metadata(item.get("generations"))
        except (TypeError, ValueError):
            continue
        if adapter is None or generations is None:
            continue
        key = (
            adapter.name,
            adapter.language or "",
            generations.trust if generations.trust is not None else -1,
            generations.program if generations.program is not None else -1,
            generations.document if generations.document is not None else -1,
            generations.index if generations.index is not None else -1,
            generations.scope or "",
        )
        authorities[key] = {
            "adapter": adapter.to_dict(),
            "generations": generations.to_dict(),
        }
    return tuple(authorities[key] for key in sorted(authorities)[:_MAX_ERROR_AUTHORITIES])


def _internal_omitted(envelope: Mapping[str, object]) -> int:
    truncation = envelope.get("truncation")
    if truncation is None:
        return 0
    value = _mapping(truncation, "truncation").get("omitted_count", 0)
    omitted = _integer(value, "truncation.omitted_count")
    if omitted < 0:
        raise ValueError("truncation.omitted_count must be non-negative")
    return omitted


def _overview_internal_omitted(data: Mapping[str, object], envelope: Mapping[str, object]) -> int:
    """Combine upstream depth and envelope truncation before final pruning."""

    depth_omitted = _integer(data.get("depth_omitted_count", 0), "depth_omitted_count")
    if depth_omitted < 0:
        raise ValueError("depth_omitted_count must be non-negative")
    return depth_omitted + _internal_omitted(envelope)


def _compact_info(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        detail = cast(Mapping[str, object], value).get("detail")
        return detail if isinstance(detail, str) and detail else None
    if value is None:
        return None
    raise TypeError("info must be a string or mapping")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"{field} must be a sequence")
    return cast(Sequence[object], value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: object, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or (not value and not allow_empty):
        raise TypeError("optional string has an invalid value")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value, "optional integer")


def _optional_nonnegative_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    parsed = _integer(value, field)
    if parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError("optional boolean has an invalid value")
    return value


def _malformed_result(
    operation: str,
    *,
    workspace: WorkspaceMetadata | None = None,
    adapter: AdapterMetadata | None = None,
    generations: GenerationMetadata | None = None,
    authorities: Sequence[Mapping[str, Any]] = (),
) -> types.CallToolResult:
    details: dict[str, Any] = {"tool": operation, "reason": "malformed_navigation_success"}
    if authorities:
        details["authorities"] = list(authorities)
    envelope = error(
        ErrorCode.UNSUPPORTED,
        details=details,
        workspace=workspace,
        adapter=adapter,
        generations=generations,
    )
    payload = envelope.to_dict()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=envelope.to_json())],
        structuredContent=payload,
        isError=False,
    )


__all__ = ("NAVIGATION_OPERATIONS", "compact_navigation_result")
