"""Language-neutral normalization for LSP symbols and locations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import unquote, urlparse


class NormalizationError(ValueError):
    """Raised when a language server returns an unusable symbol shape."""


@dataclass(frozen=True, order=True, slots=True)
class Position:
    line: int
    character: int


@dataclass(frozen=True, slots=True)
class Range:
    start: Position
    end: Position

    def __post_init__(self) -> None:
        if self.start.line < 0 or self.start.character < 0 or self.end.line < 0 or self.end.character < 0:
            raise NormalizationError("LSP range coordinates must be non-negative")
        if self.end < self.start:
            raise NormalizationError("LSP range end precedes its start")

    def contains(self, position: Position) -> bool:
        return self.start <= position < self.end


@dataclass(frozen=True, slots=True)
class Location:
    uri: str
    range: Range
    path: str | None


@dataclass(frozen=True, slots=True)
class NormalizedSymbol:
    name: str
    name_segment: str
    name_path: tuple[str, ...]
    kind: int
    location: Location
    selection_range: Range
    detail: str | None
    children: tuple[NormalizedSymbol, ...] = ()
    body_incomplete_reason: str | None = None

    def iter_depth_first(self) -> Iterator[NormalizedSymbol]:
        yield self
        for child in self.children:
            yield from child.iter_depth_first()


class ContainmentRecovery(Protocol):
    """Adapter seam for engines that return a flat or incomplete hierarchy."""

    def __call__(self, symbols: tuple[NormalizedSymbol, ...]) -> Sequence[NormalizedSymbol]: ...


class BodyCompleteness(Protocol):
    """Adapter-owned classifier for a raw symbol's body-range completeness."""

    def __call__(self, raw_symbol: Mapping[str, Any]) -> str | None: ...


def normalize_location(raw: Mapping[str, Any]) -> Location:
    uri = raw.get("uri")
    raw_range = raw.get("range")
    if not isinstance(uri, str) or not isinstance(raw_range, Mapping):
        raise NormalizationError("LSP location requires string uri and range")
    return Location(uri=uri, range=_range(raw_range), path=_file_uri_path(uri))


def normalize_document_symbols(
    raw_symbols: Sequence[Mapping[str, Any]] | None,
    *,
    document_uri: str,
    normalize_name: Callable[[str], str] | None = None,
    recover_containment: ContainmentRecovery | None = None,
    body_completeness: BodyCompleteness | None = None,
) -> tuple[NormalizedSymbol, ...]:
    """Normalize either DocumentSymbol trees or flat SymbolInformation.

    The common layer never guesses language-specific containment. An adapter
    may supply ``recover_containment`` and owns every hierarchy change it makes.
    """
    if not raw_symbols:
        return ()
    name_fn = normalize_name or (lambda name: name)
    roots = _normalize_siblings(
        raw_symbols,
        document_uri=document_uri,
        parent_path=(),
        name_fn=name_fn,
        body_completeness=body_completeness,
    )
    if recover_containment is None:
        return roots
    recovered = tuple(recover_containment(roots))
    if any(not isinstance(symbol, NormalizedSymbol) for symbol in recovered):
        raise NormalizationError("adapter containment recovery returned a non-normalized symbol")
    return recovered


def containing_symbol(symbols: Sequence[NormalizedSymbol], location: Location) -> NormalizedSymbol | None:
    """Return the narrowest normalized symbol containing a location start."""
    candidates = [
        symbol
        for root in symbols
        for symbol in root.iter_depth_first()
        if symbol.location.uri == location.uri and symbol.location.range.contains(location.range.start)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda symbol: _range_size_key(symbol.location.range))


def _normalize_siblings(
    raw_symbols: Sequence[Mapping[str, Any]],
    *,
    document_uri: str,
    parent_path: tuple[str, ...],
    name_fn: Callable[[str], str],
    body_completeness: BodyCompleteness | None,
) -> tuple[NormalizedSymbol, ...]:
    normalized_names: list[str] = []
    for raw in raw_symbols:
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise NormalizationError("LSP symbol requires a non-empty name")
        normalized = name_fn(name)
        if not normalized:
            raise NormalizationError("adapter normalized a symbol name to empty")
        normalized_names.append(normalized)
    totals = Counter(normalized_names)
    seen: Counter[str] = Counter()
    result: list[NormalizedSymbol] = []
    for raw, name in zip(raw_symbols, normalized_names, strict=True):
        overload = seen[name]
        seen[name] += 1
        segment = f"{name}[{overload}]" if totals[name] > 1 else name
        name_path = (*parent_path, segment)
        raw_location = raw.get("location")
        raw_range = raw.get("range")
        if isinstance(raw_location, Mapping):
            location = normalize_location(raw_location)
        elif isinstance(raw_range, Mapping):
            location = Location(document_uri, _range(raw_range), _file_uri_path(document_uri))
        else:
            raise NormalizationError(f"symbol {name!r} has neither location nor range")
        raw_selection = raw.get("selectionRange")
        selection = _range(raw_selection) if isinstance(raw_selection, Mapping) else location.range
        kind = raw.get("kind")
        if isinstance(kind, bool) or not isinstance(kind, int):
            raise NormalizationError(f"symbol {name!r} has an invalid kind")
        raw_children = raw.get("children", ())
        if not isinstance(raw_children, Sequence) or isinstance(raw_children, str | bytes):
            raise NormalizationError(f"symbol {name!r} children must be a sequence")
        children_mappings: list[Mapping[str, Any]] = []
        for child in raw_children:
            if not isinstance(child, Mapping):
                raise NormalizationError(f"symbol {name!r} has a non-object child")
            children_mappings.append(child)
        children = _normalize_siblings(
            children_mappings,
            document_uri=document_uri,
            parent_path=name_path,
            name_fn=name_fn,
            body_completeness=body_completeness,
        )
        detail = raw.get("detail")
        result.append(
            NormalizedSymbol(
                name=name,
                name_segment=segment,
                name_path=name_path,
                kind=kind,
                location=location,
                selection_range=selection,
                detail=str(detail) if detail is not None else None,
                children=children,
                body_incomplete_reason=body_completeness(raw) if body_completeness is not None else None,
            )
        )
    return tuple(result)


def reparent(symbol: NormalizedSymbol, parent_path: tuple[str, ...]) -> NormalizedSymbol:
    """Helper for adapter-owned containment recovery without mutating symbols."""
    name_path = (*parent_path, symbol.name_segment)
    children = tuple(reparent(child, name_path) for child in symbol.children)
    return replace(symbol, name_path=name_path, children=children)


def _position(raw: Mapping[str, Any]) -> Position:
    line = raw.get("line")
    character = raw.get("character")
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or isinstance(character, bool)
        or not isinstance(character, int)
    ):
        raise NormalizationError("LSP position requires integer line and character")
    return Position(line, character)


def _range(raw: Mapping[str, Any]) -> Range:
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise NormalizationError("LSP range requires start and end positions")
    return Range(_position(start), _position(end))


def _file_uri_path(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    return unquote(parsed.path)


def _range_size_key(value: Range) -> tuple[int, int, int, int]:
    return (
        value.end.line - value.start.line,
        value.end.character - value.start.character if value.end.line == value.start.line else value.end.character,
        -value.start.line,
        -value.start.character,
    )
