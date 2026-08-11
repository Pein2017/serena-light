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
    ExactBodyRecovery,
    LocatedCompactRecord,
    canonical_json,
    compact_range,
    compact_raw_lsp_range,
    ordered_records,
    render_bounded_overview,
    render_bounded_records,
    render_payload,
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
_DEFAULT_SUPPRESSED_DESCENDANT_KINDS = frozenset({"variable", "constant"})
_REFERENCE_COVERAGE_SAMPLE_LIMIT = 16
_UNCOVERED_REASON_BY_SCOPE = {
    "configured": "excluded_by_native_config",
    "workspace_default": "omitted_by_engine_workspace_program",
}


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

    budget = DEFAULT_MAX_ANSWER_CHARS
    authority_workspace: WorkspaceMetadata | None = None
    authority_adapter: AdapterMetadata | None = None
    authority_generations: GenerationMetadata | None = None
    error_authorities: tuple[Mapping[str, Any], ...] = ()
    exact_body_recovery: ExactBodyRecovery | None = None
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
            files = _overview_files(
                data,
                frozenset(included),
                frozenset(excluded),
                _nonnegative_integer(data.get("max_depth", 0), "max_depth"),
            )
            return render_bounded_overview(
                workspace,
                files,
                max_answer_chars=budget,
                omitted=_internal_omitted(envelope),
                error_workspace=authority_workspace,
                error_adapter=authority_adapter,
                error_generations=authority_generations,
            )

        omitted = _internal_omitted(envelope)
        coverage: Mapping[str, Any] | None = None
        if operation == "find_symbol":
            if authority_adapter is None or authority_generations is None:
                error_authorities = _symbol_authorities(data)
            records, exact_body_recovery = _symbol_records(data, envelope)
            ordered = ordered_records(records)
            omitted += max(0, len(ordered) - match_limit)
            records = ordered[:match_limit]
        elif operation == "find_referencing_symbols":
            records = _reference_records(data, include_snippets=include_snippets)
            coverage = _compact_reference_coverage(_mapping(data.get("coverage"), "coverage"))
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
            exact_body_recovery=exact_body_recovery,
        )
    except (KeyError, TypeError, ValueError):
        return _malformed_result(
            operation,
            workspace=authority_workspace,
            adapter=authority_adapter,
            generations=authority_generations,
            authorities=error_authorities,
            max_answer_chars=budget,
        )


def _overview_files(
    data: Mapping[str, object],
    included: frozenset[str],
    excluded: frozenset[str],
    max_depth: int,
) -> tuple[CompactFile, ...]:
    path = _string(data.get("relative_path"), "relative_path")
    raw_symbols = _sequence(data.get("symbols"), "symbols")
    symbols: list[CompactOverviewSymbol] = []
    for raw in raw_symbols:
        rendered = _overview_symbol(
            _mapping(raw, "symbol"),
            included,
            excluded,
            max_depth=max_depth,
            is_root=True,
        )
        if rendered is not None:
            symbols.append(rendered)
    if not symbols:
        return ()
    return (CompactFile(path, "symbols", tuple(symbols)),)


def _overview_symbol(
    raw: Mapping[str, object],
    included: frozenset[str],
    excluded: frozenset[str],
    *,
    max_depth: int,
    is_root: bool,
) -> CompactOverviewSymbol | None:
    """Filter post-order so retained descendants keep their ancestor path."""

    name = _string(raw.get("name"), "symbol.name")
    kind = _integer(raw.get("kind"), "symbol.kind")
    from serena_light.tools.compact import symbol_kind

    rendered_kind = symbol_kind(kind)
    raw_children = raw.get("children", ())
    children: list[CompactOverviewSymbol] = []
    for child in _sequence(raw_children, "symbol.children"):
        rendered = _overview_symbol(
            _mapping(child, "symbol.child"),
            included,
            excluded,
            max_depth=max_depth,
            is_root=False,
        )
        if rendered is not None:
            children.append(rendered)
    suppressed_descendant_noise = (
        max_depth > 0
        and not is_root
        and rendered_kind in _DEFAULT_SUPPRESSED_DESCENDANT_KINDS
        and rendered_kind not in included
    )
    matches = (
        not suppressed_descendant_noise
        and (not included or rendered_kind in included)
        and rendered_kind not in excluded
    )
    if matches or children:
        return CompactOverviewSymbol(name, kind, tuple(children), intrinsic_match=matches)
    return None


def _symbol_records(
    data: Mapping[str, object],
    envelope: Mapping[str, object],
) -> tuple[tuple[LocatedCompactRecord, ...], ExactBodyRecovery | None]:
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
    recovery: ExactBodyRecovery | None = None
    if (
        len(raw_items) == 1
        and len(records) == 1
        and isinstance(records[0].record, CompactSymbolMatch)
        and records[0].record.body is not None
    ):
        child_fact = raw_items[0][0].get("has_children")
        if child_fact is not None and not isinstance(child_fact, bool):
            raise ValueError("symbol.has_children must be a boolean")
        if isinstance(child_fact, bool):
            recovery = ExactBodyRecovery(child_fact, records[0].path, records[0].record.name_path)
    return tuple(records), recovery


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


def _compact_reference_coverage(raw: Mapping[str, object]) -> Mapping[str, Any]:
    """Keep only the coverage fact that changes how a reference answer reads."""

    uncovered_files = _nonnegative_integer(raw.get("uncovered_files"), "coverage.uncovered_files")
    if uncovered_files == 0:
        return {"complete": True}

    sample = _mapping(raw.get("uncovered_sample"), "coverage.uncovered_sample")
    if _nonnegative_integer(sample.get("total"), "coverage.uncovered_sample.total") != uncovered_files:
        raise ValueError("coverage sample total must equal uncovered_files")
    reason = _uncovered_reason(raw)
    evidence: list[dict[str, str]] = []
    for value in _sequence(sample.get("items"), "coverage.uncovered_sample.items"):
        if isinstance(value, str):
            path = _string(value, "coverage.uncovered_sample.path")
            item_reason = reason
        else:
            item = _mapping(value, "coverage.uncovered_sample.item")
            path = _string(item.get("path"), "coverage.uncovered_sample.item.path")
            item_reason = _string(item.get("reason"), "coverage.uncovered_sample.item.reason")
        evidence.append({"path": path, "reason": item_reason})
    if len(evidence) > uncovered_files:
        raise ValueError("coverage sample cannot exceed uncovered_files")
    if not evidence:
        raise ValueError("incomplete coverage requires evidence")
    sample_omitted = _nonnegative_integer(sample.get("omitted"), "coverage.uncovered_sample.omitted")
    if sample_omitted != uncovered_files - len(evidence):
        raise ValueError("coverage sample omitted count is inconsistent")
    if len({(item["path"], item["reason"]) for item in evidence}) != len(evidence):
        raise ValueError("coverage sample must not repeat evidence")
    retained = sorted(evidence, key=lambda item: (item["path"], item["reason"]))[:_REFERENCE_COVERAGE_SAMPLE_LIMIT]
    return {
        "complete": False,
        "uncovered_files": uncovered_files,
        "sample": retained,
        "omitted": uncovered_files - len(retained),
    }


def _uncovered_reason(raw: Mapping[str, object]) -> str:
    scope_kind = _string(raw.get("scope_kind"), "coverage.scope_kind")
    try:
        return _UNCOVERED_REASON_BY_SCOPE[scope_kind]
    except KeyError as exc:
        raise ValueError("coverage.scope_kind is unsupported") from exc


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


def _nonnegative_integer(value: object, field: str) -> int:
    parsed = _integer(value, field)
    if parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


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
    max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
) -> types.CallToolResult:
    budget = validate_max_answer_chars(max_answer_chars)
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
    return render_payload(_bound_malformed_navigation_error(envelope.to_dict(), budget))


def _bound_malformed_navigation_error(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    """Keep the closed malformed reason while dropping unbounded optional context."""

    if len(canonical_json(payload)) <= budget:
        return payload
    for field in ("workspace", "adapter", "generations"):
        payload.pop(field, None)
        if len(canonical_json(payload)) <= budget:
            return payload
    error_value = payload.get("error")
    if isinstance(error_value, dict):
        details = error_value.get("details")
        if isinstance(details, dict):
            details.pop("authorities", None)
            if len(canonical_json(payload)) <= budget:
                return payload
            details.pop("tool", None)
            if len(canonical_json(payload)) <= budget:
                return payload
    raise ValueError("minimum malformed navigation error exceeds the public answer budget")


__all__ = ("NAVIGATION_OPERATIONS", "compact_navigation_result")
