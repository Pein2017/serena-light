"""AST-based recovery of module-level Python assignment-statement ranges.

Pyright can report a module variable or constant document symbol whose
``range`` covers only the identifier rather than the complete assignment
statement (``range == selectionRange``).  This module recovers the complete
statement range from the same verified :class:`FileSnapshot`, using Python's
own ``ast`` positions rather than line-based expansion, so a recovered range
never absorbs a trailing comment or an adjacent statement.

This module is Python-source-specific but otherwise adapter-neutral: it knows
nothing about Pyright's process, capabilities, or configuration.  The owning
Pyright adapter calls it and is the only production caller.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionError, PositionMapper

# LSP SymbolKind values that Pyright uses for module variables and constants.
_VARIABLE_SYMBOL_KIND = 13
_CONSTANT_SYMBOL_KIND = 14
_RECOVERABLE_KINDS = frozenset({_VARIABLE_SYMBOL_KIND, _CONSTANT_SYMBOL_KIND})


class AssignmentRecoveryReason:
    """Stable reason codes for a symbol recovery could not complete."""

    SYNTAX_INVALID = "syntax_invalid"
    NO_ENCLOSING_ASSIGNMENT = "no_enclosing_assignment"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class UnresolvedAssignmentSymbol:
    """One module-level variable/constant symbol recovery could not complete.

    ``range`` is the original, still identifier-only LSP range Pyright
    reported; it is unchanged in the corresponding entry of
    :attr:`AssignmentRecoveryResult.raw_symbols` so ordinary lookup keeps an
    accurately labelled identifier range.
    """

    name: str
    range: Mapping[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class AssignmentRecoveryResult:
    """Raw document symbols with recoverable module-level ranges expanded."""

    raw_symbols: tuple[Mapping[str, Any], ...]
    unresolved: tuple[UnresolvedAssignmentSymbol, ...]

    def incomplete_range_reason(self, *, name: str, selection_range: Mapping[str, Any]) -> str | None:
        """Return the recovery-failure reason for one exact symbol, if any.

        A caller that renders ``include_body=True`` MUST check this before
        treating a sliced range as a complete body: a non-``None`` result
        means the identifier range in ``raw_symbols`` was left unchanged and
        must not be advertised as a complete assignment body.
        """

        for item in self.unresolved:
            if item.name == name and item.range == selection_range:
                return item.reason
        return None

    def body_incomplete_reason(self, raw_symbol: Mapping[str, Any]) -> str | None:
        """Classify one raw symbol for the language-neutral normalization seam."""

        name = raw_symbol.get("name")
        selection_range = raw_symbol.get("selectionRange")
        if not isinstance(name, str) or not isinstance(selection_range, Mapping):
            return None
        return self.incomplete_range_reason(name=name, selection_range=selection_range)


@dataclass(frozen=True, slots=True)
class _Candidate:
    start_offset: int
    end_offset: int
    target_names: tuple[tuple[str, int, int], ...]


def recover_python_module_assignment_symbols(
    raw_symbols: Sequence[Mapping[str, Any]] | None,
    *,
    snapshot: FileSnapshot,
    position_encoding: PositionEncoding,
) -> AssignmentRecoveryResult:
    """Recover identifier-only module-level ``Assign``/``AnnAssign`` ranges.

    Only the top-level (module-scope) entries of ``raw_symbols`` are
    inspected; nested ``children`` are returned unchanged, and a symbol whose
    ``range`` already differs from its ``selectionRange`` is left untouched
    because the server already reported a complete range.  Recovery fails
    closed: an ambiguous, syntax-invalid, or otherwise unsupported case keeps
    the original identifier-only range and is reported in ``unresolved``
    instead of being silently expanded.
    """

    if not raw_symbols:
        return AssignmentRecoveryResult(raw_symbols=tuple(raw_symbols or ()), unresolved=())

    mapper = PositionMapper(snapshot, position_encoding)
    try:
        candidates = _module_level_candidates(snapshot.text)
        syntax_invalid = False
    except SyntaxError:
        candidates = ()
        syntax_invalid = True

    recovered: list[Mapping[str, Any]] = []
    unresolved: list[UnresolvedAssignmentSymbol] = []
    for raw in raw_symbols:
        target = _identifier_only_candidate_target(raw)
        if target is None:
            recovered.append(raw)
            continue
        name, raw_range, raw_selection = target
        if syntax_invalid:
            unresolved.append(UnresolvedAssignmentSymbol(name, raw_range, AssignmentRecoveryReason.SYNTAX_INVALID))
            recovered.append(raw)
            continue
        try:
            id_start = mapper.lsp_to_text_offset(_lsp_position(raw_selection["start"]))
            id_end = mapper.lsp_to_text_offset(_lsp_position(raw_selection["end"]))
        except (PositionError, KeyError, TypeError, ValueError):
            unresolved.append(
                UnresolvedAssignmentSymbol(name, raw_range, AssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT)
            )
            recovered.append(raw)
            continue
        statement = _select_enclosing_statement(candidates, name=name, id_start=id_start, id_end=id_end)
        if isinstance(statement, _Candidate):
            new_range = {
                "start": _lsp_dict(mapper.text_offset_to_lsp(statement.start_offset)),
                "end": _lsp_dict(mapper.text_offset_to_lsp(statement.end_offset)),
            }
            recovered.append({**raw, "range": new_range})
        else:
            unresolved.append(UnresolvedAssignmentSymbol(name, raw_range, statement))
            recovered.append(raw)
    return AssignmentRecoveryResult(raw_symbols=tuple(recovered), unresolved=tuple(unresolved))


def _identifier_only_candidate_target(
    raw: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any], Mapping[str, Any]] | None:
    kind = raw.get("kind")
    raw_range = raw.get("range")
    raw_selection = raw.get("selectionRange")
    name = raw.get("name")
    if (
        kind not in _RECOVERABLE_KINDS
        or not isinstance(raw_range, Mapping)
        or not isinstance(raw_selection, Mapping)
        or raw_range != raw_selection
        or not isinstance(name, str)
        or not name
    ):
        return None
    return name, raw_range, raw_selection


def _select_enclosing_statement(
    candidates: Sequence[_Candidate],
    *,
    name: str,
    id_start: int,
    id_end: int,
) -> _Candidate | str:
    """Return the unique enclosing statement, or an unresolved reason code."""

    enclosing = [
        candidate
        for candidate in candidates
        if any(
            target_name == name
            and id_start < id_end
            and target_start <= id_start
            and id_end <= target_end
            for target_name, target_start, target_end in candidate.target_names
        )
    ]
    if len(enclosing) == 1:
        return enclosing[0]
    if len(enclosing) > 1:
        return AssignmentRecoveryReason.AMBIGUOUS
    same_name = [
        candidate
        for candidate in candidates
        if any(target_name == name for target_name, _start, _end in candidate.target_names)
    ]
    return (
        AssignmentRecoveryReason.AMBIGUOUS
        if len(same_name) > 1
        else AssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
    )


def _module_level_candidates(text: str) -> tuple[_Candidate, ...]:
    tree = ast.parse(text)
    boundaries = _line_boundaries(text)
    candidates: list[_Candidate] = []
    for node in _module_executed_statements(tree.body):
        if isinstance(node, ast.Assign):
            target_nodes = [name_node for target in node.targets for name_node in _flatten_names(target)]
        elif isinstance(node, ast.AnnAssign):
            target_nodes = list(_flatten_names(node.target))
        else:
            continue
        if not target_nodes:
            continue
        start_offset = _ast_offset_to_text_offset(text, boundaries, node.lineno, node.col_offset)
        assert node.end_lineno is not None
        assert node.end_col_offset is not None
        end_offset = _ast_offset_to_text_offset(text, boundaries, node.end_lineno, node.end_col_offset)
        target_names = tuple(
            (
                name_node.id,
                _ast_offset_to_text_offset(text, boundaries, name_node.lineno, name_node.col_offset),
                _ast_offset_to_text_offset(
                    text,
                    boundaries,
                    _require(name_node.end_lineno),
                    _require(name_node.end_col_offset),
                ),
            )
            for name_node in target_nodes
        )
        candidates.append(_Candidate(start_offset, end_offset, target_names))
    return tuple(candidates)


def _module_executed_statements(statements: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements executed in the module scope, including control-flow suites.

    This intentionally walks only suites that preserve module execution scope.
    In particular, it does not generically traverse AST children, which keeps
    assignments in functions, classes, lambdas, and comprehensions out of the
    recovery candidate set.
    """

    for node in statements:
        yield node
        if isinstance(node, ast.If):
            yield from _module_executed_statements(node.body)
            yield from _module_executed_statements(node.orelse)
        elif isinstance(node, ast.Try | ast.TryStar):
            yield from _module_executed_statements(node.body)
            for handler in node.handlers:
                yield from _module_executed_statements(handler.body)
            yield from _module_executed_statements(node.orelse)
            yield from _module_executed_statements(node.finalbody)
        elif isinstance(node, ast.With | ast.AsyncWith):
            yield from _module_executed_statements(node.body)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.While):
            yield from _module_executed_statements(node.body)
            yield from _module_executed_statements(node.orelse)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _module_executed_statements(case.body)


def _flatten_names(target: ast.expr) -> Iterator[ast.Name]:
    if isinstance(target, ast.Name):
        yield target
    elif isinstance(target, ast.Starred):
        yield from _flatten_names(target.value)
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _flatten_names(element)
    # Attribute and Subscript targets (``obj.x = 1``, ``obj[0] = 1``) are not
    # supported assignment recovery targets and are intentionally skipped.


def _require(value: int | None) -> int:
    if value is None:
        raise ValueError("ast node is missing an end position")
    return value


def _line_boundaries(text: str) -> tuple[tuple[int, int], ...]:
    """Return (content_start, content_end) offsets per physical line.

    Mirrors ``FileSnapshot``'s own CR/LF/CRLF splitting so ``ast`` line
    numbers (which follow the same universal-newline convention) index this
    table directly.
    """

    starts = [0]
    content_ends: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r" and index + 1 < length and text[index + 1] == "\n":
            content_ends.append(index)
            starts.append(index + 2)
            index += 2
        elif char == "\n" or char == "\r":
            content_ends.append(index)
            starts.append(index + 1)
            index += 1
        else:
            index += 1
    content_ends.append(length)
    return tuple(zip(starts, content_ends, strict=True))


def _ast_offset_to_text_offset(
    text: str,
    boundaries: Sequence[tuple[int, int]],
    lineno: int,
    col_offset: int,
) -> int:
    """Convert an ast (1-based line, UTF-8 byte column) into a text offset."""

    if lineno < 1 or lineno > len(boundaries):
        raise ValueError(f"ast line {lineno} is outside the parsed source")
    content_start, content_end = boundaries[lineno - 1]
    line_text = text[content_start:content_end]
    prefix = line_text.encode("utf-8")[:col_offset]
    return content_start + len(prefix.decode("utf-8"))


def _lsp_position(raw: Mapping[str, Any]) -> LspPosition:
    line = raw["line"]
    character = raw["character"]
    invalid_line = isinstance(line, bool) or not isinstance(line, int)
    invalid_character = isinstance(character, bool) or not isinstance(character, int)
    if invalid_line or invalid_character:
        raise ValueError("LSP position requires integer line and character")
    return LspPosition(line, character)


def _lsp_dict(position: LspPosition) -> dict[str, int]:
    return {"line": position.line, "character": position.character}
