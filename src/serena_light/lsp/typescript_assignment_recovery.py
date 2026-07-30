"""Server-syntax-backed recovery for TypeScript variable statements.

The pinned TypeScript server reports identifier-only ``documentSymbol``
ranges for top-level destructured bindings and identifier-start ranges for
plain bindings.  Its ``selectionRange`` request, however, returns the exact
syntax ancestry from the identifier through the containing variable statement.
This module consumes that evidence against the same verified snapshot; it
never implements a second JavaScript parser or guesses across line boundaries.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionError, PositionMapper

_VARIABLE_SYMBOL_KIND = 13
_CONSTANT_SYMBOL_KIND = 14
_RECOVERABLE_KINDS = frozenset({_VARIABLE_SYMBOL_KIND, _CONSTANT_SYMBOL_KIND})
_DECLARATION_PREFIX = re.compile(r"(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+")


class TypeScriptAssignmentRecoveryReason:
    """Stable fail-closed reason codes for incomplete variable bodies."""

    SELECTION_RANGE_UNAVAILABLE = "selection_range_unavailable"
    NO_ENCLOSING_ASSIGNMENT = "no_enclosing_assignment"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class UnresolvedAssignmentSymbol:
    name: str
    range: Mapping[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class AssignmentRecoveryResult:
    raw_symbols: tuple[Mapping[str, Any], ...]
    unresolved: tuple[UnresolvedAssignmentSymbol, ...]

    def incomplete_range_reason(self, *, name: str, selection_range: Mapping[str, Any]) -> str | None:
        for item in self.unresolved:
            if item.name == name and item.range == selection_range:
                return item.reason
        return None

    def body_incomplete_reason(self, raw_symbol: Mapping[str, Any]) -> str | None:
        name = raw_symbol.get("name")
        selection_range = raw_symbol.get("selectionRange")
        if not isinstance(name, str) or not isinstance(selection_range, Mapping):
            return None
        return self.incomplete_range_reason(name=name, selection_range=selection_range)


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    raw: Mapping[str, Any]
    raw_range: Mapping[str, Any]
    selection: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _SyntaxRange:
    raw: Mapping[str, Any]
    start_offset: int
    end_offset: int


def assignment_recovery_positions(
    raw_symbols: Sequence[Mapping[str, Any]] | None,
) -> tuple[Mapping[str, int], ...]:
    """Return identifier starts whose variable ranges omit declaration syntax."""

    return tuple(candidate.selection["start"] for candidate in _candidates(raw_symbols))


def recover_typescript_top_level_variable_symbols(
    raw_symbols: Sequence[Mapping[str, Any]] | None,
    *,
    selection_ranges: Sequence[Mapping[str, Any]] | None,
    snapshot: FileSnapshot,
    position_encoding: PositionEncoding,
) -> AssignmentRecoveryResult:
    """Expand identifier-start bindings using matching LSP syntax chains.

    ``selection_ranges`` must be the ordered response to a request whose
    positions came from :func:`assignment_recovery_positions`.  Missing,
    malformed, or ambiguous evidence leaves the original semantic server range
    unchanged so ``include_body=true`` can fail typed.
    """

    symbols = tuple(raw_symbols or ())
    candidates = _candidates(symbols)
    evidence = tuple(selection_ranges or ())
    mapper = PositionMapper(snapshot, position_encoding)
    recovered_by_identity: dict[int, Mapping[str, Any]] = {}
    unresolved: list[UnresolvedAssignmentSymbol] = []

    position_counts: dict[tuple[int, int], int] = {}
    for candidate in candidates:
        key = _position_key(candidate.selection["start"])
        position_counts[key] = position_counts.get(key, 0) + 1
    if any(count > 1 for count in position_counts.values()):
        for candidate in candidates:
            reason = (
                TypeScriptAssignmentRecoveryReason.AMBIGUOUS
                if position_counts[_position_key(candidate.selection["start"])] > 1
                else TypeScriptAssignmentRecoveryReason.SELECTION_RANGE_UNAVAILABLE
            )
            unresolved.append(UnresolvedAssignmentSymbol(candidate.name, candidate.selection, reason))
        return AssignmentRecoveryResult(symbols, tuple(unresolved))

    if len(evidence) != len(candidates):
        for candidate in candidates:
            unresolved.append(
                UnresolvedAssignmentSymbol(
                    candidate.name,
                    candidate.selection,
                    TypeScriptAssignmentRecoveryReason.SELECTION_RANGE_UNAVAILABLE,
                )
            )
        return AssignmentRecoveryResult(symbols, tuple(unresolved))

    for candidate, chain in zip(candidates, evidence, strict=True):
        try:
            recovered_range = _recover_range(candidate, chain, mapper, snapshot.text)
        except (KeyError, PositionError, TypeError, ValueError):
            recovered_range = TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
        if isinstance(recovered_range, str):
            unresolved.append(UnresolvedAssignmentSymbol(candidate.name, candidate.selection, recovered_range))
        else:
            recovered_by_identity[id(candidate.raw)] = {**candidate.raw, "range": recovered_range}

    return AssignmentRecoveryResult(
        tuple(recovered_by_identity.get(id(raw), raw) for raw in symbols),
        tuple(unresolved),
    )


def _candidates(raw_symbols: Sequence[Mapping[str, Any]] | None) -> tuple[_Candidate, ...]:
    result: list[_Candidate] = []
    for raw in raw_symbols or ():
        kind = raw.get("kind")
        raw_range = raw.get("range")
        selection = raw.get("selectionRange")
        name = raw.get("name")
        if (
            kind not in _RECOVERABLE_KINDS
            or not isinstance(raw_range, Mapping)
            or not isinstance(selection, Mapping)
            or not isinstance(name, str)
            or not name
            or not _position_mapping(raw_range.get("start"))
            or not _position_mapping(selection.get("start"))
            or _position_key(raw_range["start"]) != _position_key(selection["start"])
        ):
            continue
        result.append(_Candidate(name, raw, raw_range, selection))
    return tuple(result)


def _recover_range(
    candidate: _Candidate,
    chain: Mapping[str, Any],
    mapper: PositionMapper,
    text: str,
) -> Mapping[str, Any] | str:
    raw_start, raw_end = _offsets(candidate.raw_range, mapper)
    selection_start, selection_end = _offsets(candidate.selection, mapper)
    # The server's reported name is untrusted evidence about *which* binding a
    # position anchors; only the exact snapshot text at that position proves it.
    if text[selection_start:selection_end] != candidate.name:
        return TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
    syntax_ranges = _syntax_chain(chain, mapper)
    if not syntax_ranges:
        return TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
    first = syntax_ranges[0]
    if (
        raw_start != selection_start
        or raw_end < selection_end
        or first.start_offset != selection_start
        or first.end_offset < selection_end
    ):
        return TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT

    bindings = [
        item
        for item in syntax_ranges[1:]
        if item.start_offset <= selection_start
        and item.end_offset >= selection_end
        and _is_binding_pattern(text[item.start_offset : item.end_offset])
    ]
    if len(bindings) > 1:
        return TypeScriptAssignmentRecoveryReason.AMBIGUOUS
    binding = bindings[0] if bindings else None
    binding_start = binding.start_offset if binding is not None else selection_start

    statements: list[tuple[_SyntaxRange, int]] = []
    for item in syntax_ranges:
        if item.start_offset >= binding_start or item.end_offset < raw_end:
            continue
        prefix = text[item.start_offset : binding_start]
        if not _DECLARATION_PREFIX.fullmatch(prefix):
            continue
        end_offset = item.end_offset
        if binding is not None:
            suffix = text[binding.end_offset : end_offset]
            if not suffix.lstrip().startswith("="):
                continue
        elif text[raw_end:end_offset].strip() not in {"", ";"}:
            continue
        statements.append((item, end_offset))
    if len(statements) != 1:
        return (
            TypeScriptAssignmentRecoveryReason.AMBIGUOUS
            if len(statements) > 1
            else TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
        )

    statement, end_offset = statements[0]
    return {
        "start": _lsp_dict(mapper.text_offset_to_lsp(statement.start_offset)),
        "end": _lsp_dict(mapper.text_offset_to_lsp(end_offset)),
    }


def _syntax_chain(raw: Mapping[str, Any], mapper: PositionMapper) -> tuple[_SyntaxRange, ...]:
    result: list[_SyntaxRange] = []
    current: Mapping[str, Any] | None = raw
    seen: set[int] = set()
    while current is not None:
        identity = id(current)
        if identity in seen:
            raise ValueError("selection range parent cycle")
        seen.add(identity)
        raw_range = current.get("range")
        if not isinstance(raw_range, Mapping):
            raise ValueError("selection range is missing a range")
        try:
            start, end = _offsets(raw_range, mapper)
        except PositionError:
            if result:
                break
            raise
        if result and (start > result[-1].start_offset or end < result[-1].end_offset):
            break
        result.append(_SyntaxRange(raw_range, start, end))
        parent = current.get("parent")
        if parent is None:
            current = None
        elif isinstance(parent, Mapping):
            current = parent
        else:
            raise ValueError("selection range parent is not an object")
    return tuple(result)


def _offsets(raw_range: Mapping[str, Any], mapper: PositionMapper) -> tuple[int, int]:
    start = raw_range.get("start")
    end = raw_range.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValueError("range endpoints are missing")
    return mapper.lsp_to_text_offset(_lsp_position(start)), mapper.lsp_to_text_offset(_lsp_position(end))


def _is_binding_pattern(value: str) -> bool:
    return len(value) >= 2 and (value[0], value[-1]) in {("[", "]"), ("{", "}")}


def _position_mapping(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw = cast(Mapping[str, object], value)
    line = raw.get("line")
    character = raw.get("character")
    return (
        isinstance(line, int)
        and not isinstance(line, bool)
        and line >= 0
        and isinstance(character, int)
        and not isinstance(character, bool)
        and character >= 0
    )


def _position_key(value: object) -> tuple[int, int]:
    if not _position_mapping(value):
        raise ValueError("LSP position requires non-negative integer coordinates")
    raw = cast(Mapping[str, int], value)
    return raw["line"], raw["character"]


def _lsp_position(raw: Mapping[str, Any]) -> LspPosition:
    if not _position_mapping(raw):
        raise ValueError("LSP position requires non-negative integer coordinates")
    return LspPosition(raw["line"], raw["character"])


def _lsp_dict(position: LspPosition) -> dict[str, int]:
    return {"line": position.line, "character": position.character}
