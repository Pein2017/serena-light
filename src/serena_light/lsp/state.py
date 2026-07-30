"""Thread-safe, push-only LSP document and diagnostic state.

The LSP core owns ordering; this module only records the facts it is given.  In
particular, it deliberately has no pull-diagnostic requests, wait loops, or
workspace registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from types import MappingProxyType


class DiagnosticsState(StrEnum):
    """How a diagnostic publication relates to a document generation."""

    MISSING = "missing"
    CLEAN = "clean"
    FINDINGS = "findings"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class Generations:
    """Low-level LSP generations; workspace scope generations live separately."""

    source: int = 0
    index: int = 0
    diagnostics: int = 0


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    uri: str
    path: Path
    version: int | None
    generation: int


@dataclass(frozen=True, slots=True)
class DiagnosticsSnapshot:
    """An immutable published diagnostic snapshot, or its absence/staleness."""

    state: DiagnosticsState
    uri: str
    path: Path | None
    version: int | None
    generation: int
    diagnostics_generation: int | None
    diagnostics: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _Publication:
    document: DocumentSnapshot
    diagnostics_generation: int
    diagnostics: tuple[object, ...]


def _freeze(value: object) -> object:
    """Make JSON-shaped diagnostics safe to retain and hand back to callers.

    ``lsprotocol`` models are attrs classes; converting their public attributes
    to immutable mappings also prevents later caller mutation from changing a
    published result.  The protocol conversion layer may pass ordinary mapping
    diagnostics, which follow the same path.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return _freeze(asdict(value))
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, str | bytes | int | float | bool | type(None)):
        return value
    if isinstance(value, Sequence):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)

    attrs = getattr(type(value), "__attrs_attrs__", None)
    if attrs is not None:
        return MappingProxyType({attribute.name: _freeze(getattr(value, attribute.name)) for attribute in attrs})
    slots = getattr(type(value), "__slots__", ())
    if slots:
        names = (slots,) if isinstance(slots, str) else slots
        return MappingProxyType({name: _freeze(getattr(value, name)) for name in names if hasattr(value, name)})
    if hasattr(value, "__dict__"):
        return _freeze(vars(value))
    raise TypeError(f"diagnostic {value!r} cannot be frozen")


class LspState:
    """Own document versions and push diagnostic publications for one adapter.

    A publication must name the current document generation.  This makes an
    unversioned LSP publication safe: its caller still supplies the generation
    captured when it requested analysis.  Older versions or generations return
    ``False`` and leave the newest state intact.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._generations = Generations()
        self._documents: dict[str, DocumentSnapshot] = {}
        self._publications: dict[str, _Publication] = {}

    @property
    def generations(self) -> Generations:
        with self._lock:
            return self._generations

    def advance_source_generation(self) -> Generations:
        return self._advance("source")

    def advance_index_generation(self) -> Generations:
        return self._advance("index")

    def observe_index_generation(self, generation: int) -> Generations:
        """Record the newest configured-program generation indexed by the server."""

        if generation < 0:
            raise ValueError("index generation must be non-negative")
        with self._lock:
            if generation > self._generations.index:
                self._generations = replace(self._generations, index=generation)
            return self._generations

    def _advance(self, name: str) -> Generations:
        with self._lock:
            self._generations = replace(self._generations, **{name: getattr(self._generations, name) + 1})
            return self._generations

    def update_document(self, *, uri: str, path: Path, version: int | None) -> DocumentSnapshot | None:
        """Record a newer document observation, returning ``None`` if it is old."""

        with self._lock:
            previous = self._documents.get(uri)
            if previous is not None and not self._is_newer_version(version, previous.version):
                return None
            generation = 1 if previous is None else previous.generation + 1
            document = DocumentSnapshot(uri=uri, path=path, version=version, generation=generation)
            self._documents[uri] = document
            return document

    @staticmethod
    def _is_newer_version(candidate: int | None, current: int | None) -> bool:
        if current is None:
            return candidate is not None or candidate is None
        return candidate is not None and candidate > current

    def document(self, uri: str) -> DocumentSnapshot | None:
        with self._lock:
            return self._documents.get(uri)

    def reset_documents(self) -> None:
        """Drop process-owned document and diagnostic state after a restart.

        Source/index/diagnostic counters stay monotonic across a replacement
        language-server process, but no document version or push publication
        belongs to that new process until it receives a fresh ``didOpen``.
        """

        with self._lock:
            self._documents.clear()
            self._publications.clear()

    def publish_diagnostics(
        self,
        *,
        uri: str,
        path: Path,
        version: int | None,
        generation: int,
        diagnostics: Sequence[object],
    ) -> bool:
        """Store a diagnostic publication only when it matches the live document."""

        with self._lock:
            document = self._documents.get(uri)
            if document is None or document.path != path or generation != document.generation:
                return False
            if version is not None and document.version is not None and version != document.version:
                return False
            frozen = tuple(_freeze(diagnostic) for diagnostic in diagnostics)
            diagnostics_generation = self._generations.diagnostics + 1
            self._generations = replace(self._generations, diagnostics=diagnostics_generation)
            self._publications[uri] = _Publication(document, diagnostics_generation, frozen)
            return True

    def diagnostics_snapshot(self, uri: str, *, generation: int | None = None) -> DiagnosticsSnapshot:
        """Return missing, current clean/findings, or an explicitly stale snapshot."""

        with self._lock:
            document = self._documents.get(uri)
            publication = self._publications.get(uri)
            expected_generation = generation if generation is not None else (document.generation if document else 0)
            if publication is None:
                return DiagnosticsSnapshot(
                    DiagnosticsState.MISSING,
                    uri,
                    document.path if document else None,
                    document.version if document else None,
                    expected_generation,
                    None,
                    (),
                )
            state = (
                DiagnosticsState.STALE
                if publication.document.generation != expected_generation
                else (DiagnosticsState.FINDINGS if publication.diagnostics else DiagnosticsState.CLEAN)
            )
            return DiagnosticsSnapshot(
                state,
                uri,
                publication.document.path,
                publication.document.version,
                publication.document.generation,
                publication.diagnostics_generation,
                publication.diagnostics,
            )
