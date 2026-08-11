"""Transport-neutral configured-program global symbol lookup.

The core intentionally accepts only bounded ``workspace/symbol`` candidate
batches and candidate-file ``documentSymbol`` responses.  It has no inventory
or filesystem-enumeration dependency, so it cannot grow an O(files) fallback.
Adapters retain transport, readiness, native-program attribution, and
language-specific normalization ownership through the protocols below.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse

from serena_light.lsp.normalize import (
    BodyCompleteness,
    ContainmentRecovery,
    NormalizedSymbol,
    Position,
    Range,
    normalize_document_symbols,
)
from serena_light.lsp.positions import (
    FileSnapshot,
    LspPosition,
    PositionEncoding,
    PositionError,
    PublicPositionRenderer,
)
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    GenerationMetadata,
    JsonValue,
    RetryMetadata,
    ToolEnvelope,
    TruncationMetadata,
    WorkspaceMetadata,
    error,
    success,
)

type RawSymbol = Mapping[str, Any]

MAX_ADAPTERS = 2
MAX_CANDIDATES_PER_ADAPTER = 256
DEFAULT_MAX_ANSWER_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class ConfiguredProgramScope:
    """The exact native program searched by one adapter.

    Paths are retained for constant-time candidate authorization but are not
    emitted individually.  The public response advertises their bounded
    count/digest projection, native project kind, and selected config.
    """

    paths: tuple[str, ...]
    project_kind: str
    selected_config_path: str | None = None
    file_count: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        normalized = tuple(sorted({_relative_path(path) for path in self.paths}))
        if not self.project_kind:
            raise ValueError("project_kind must be non-empty")
        selected = self.selected_config_path
        if selected is not None:
            selected = _relative_path(selected)
        object.__setattr__(self, "paths", normalized)
        object.__setattr__(self, "selected_config_path", selected)
        object.__setattr__(self, "file_count", len(normalized))
        object.__setattr__(
            self,
            "sha256",
            hashlib.sha256("\0".join(normalized).encode("utf-8", "surrogateescape")).hexdigest(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": "configured_program",
            "project_kind": self.project_kind,
            "selected_config_path": self.selected_config_path,
            "file_count": self.file_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class GlobalAdapterState:
    """One adapter's current capability, readiness, scope, and generations."""

    workspace: WorkspaceMetadata
    adapter: AdapterMetadata
    generations: GenerationMetadata
    configured_program: ConfiguredProgramScope
    workspace_symbols_supported: bool
    global_ready: bool
    phase: str
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.adapter.name or not self.phase:
            raise ValueError("adapter name and phase must be non-empty")
        if self.generations.program is None or self.generations.index is None:
            raise ValueError("global state requires program and index generations")
        if self.generations.scope not in {None, "configured_program"}:
            raise ValueError("global state must advertise configured_program scope")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class WorkspaceSymbolBatch:
    """A provider-bounded response to exactly one ``workspace/symbol`` call."""

    raw_candidates: Sequence[RawSymbol]
    generations: GenerationMetadata
    truncated: bool = False
    omitted_count: int = 0

    def __post_init__(self) -> None:
        if self.omitted_count < 0 or (not self.truncated and self.omitted_count):
            raise ValueError("workspace candidate truncation is inconsistent")


@dataclass(frozen=True, slots=True)
class DocumentSymbolBatch:
    """One candidate file's document-symbol response at the same generation."""

    relative_path: str
    uri: str
    raw_symbols: Sequence[RawSymbol] | None
    generations: GenerationMetadata
    snapshot: FileSnapshot
    position_encoding: PositionEncoding = PositionEncoding.UTF16
    normalize_name: Callable[[str], str] | None = None
    recover_containment: ContainmentRecovery | None = None
    body_completeness: BodyCompleteness | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", _relative_path(self.relative_path))
        if not self.uri:
            raise ValueError("document URI must be non-empty")


class GlobalSymbolProvider(Protocol):
    """Injected adapter/transport boundary for global symbol lookup.

    No method exposes inventory iteration.  Implementations must issue one
    bounded workspace-symbol query and document-symbol requests only for the
    relative paths handed back by this core.
    """

    def global_symbol_state(self) -> GlobalAdapterState: ...

    def workspace_symbols(self, query: str, *, max_results: int) -> WorkspaceSymbolBatch: ...

    def document_symbols(self, relative_path: str, uri: str) -> DocumentSymbolBatch: ...


@dataclass(frozen=True, slots=True)
class _NamePathPattern:
    components: tuple[str, ...]
    absolute: bool

    @property
    def expression(self) -> str:
        return f"{'/' if self.absolute else ''}{'/'.join(self.components)}"

    @property
    def query(self) -> str:
        return self.components[-1]


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    kind: int
    relative_path: str
    uri: str
    range: Range | None


class GlobalSymbolService:
    """Merge at most the fixed Python and TypeScript adapter providers."""

    def __init__(self, providers: Sequence[GlobalSymbolProvider]) -> None:
        if not providers or len(providers) > MAX_ADAPTERS:
            raise ValueError(f"global lookup requires one or two fixed adapters (maximum {MAX_ADAPTERS})")
        states = [(provider, provider.global_symbol_state()) for provider in providers]
        names = [state.adapter.name for _, state in states]
        if len(names) != len(set(names)):
            raise ValueError("global adapter names must be unique")
        self._providers = tuple(provider for provider, _ in sorted(states, key=lambda item: _adapter_key(item[1])))

    def find_symbol(
        self,
        name_path: str | Sequence[str],
        *,
        substring_matching: bool = False,
        include_body: bool = False,
        include_info: bool = False,
        max_candidates_per_adapter: int = 128,
        max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
    ) -> ToolEnvelope:
        pattern = _parse_name_path(name_path)
        if (
            pattern is None
            or max_answer_chars <= 0
            or not 1 <= max_candidates_per_adapter <= MAX_CANDIDATES_PER_ADAPTER
        ):
            return error(
                ErrorCode.INVALID_INPUT,
                details={
                    "field": "name_path, max_candidates_per_adapter, or max_answer_chars",
                    "max_candidates_per_adapter": MAX_CANDIDATES_PER_ADAPTER,
                },
            )

        states = tuple(provider.global_symbol_state() for provider in self._providers)
        workspace = _common_workspace(states)
        if workspace is None:
            return error(ErrorCode.INVALID_INPUT, details={"field": "provider workspace identity"})
        unavailable = tuple(state for state in states if not state.workspace_symbols_supported)
        if unavailable:
            return error(
                ErrorCode.UNSUPPORTED,
                details={
                    "operation": "global_find_symbol",
                    "scope": "configured_program",
                    "adapters": [_state_data(state) for state in unavailable],
                },
                workspace=workspace,
            )
        not_ready = tuple(state for state in states if not state.global_ready)
        if not_ready:
            retry_values = [state.retry_after_seconds for state in not_ready if state.retry_after_seconds is not None]
            return error(
                ErrorCode.NOT_READY,
                retry=RetryMetadata(
                    retryable=True,
                    retry_after_seconds=min(retry_values) if retry_values else None,
                ),
                details={
                    "scope": "configured_program",
                    "adapters": [_state_data(state) for state in not_ready],
                },
                workspace=workspace,
            )

        rendered: list[dict[str, Any]] = []
        omitted = 0
        for provider, state in zip(self._providers, states, strict=True):
            batch = provider.workspace_symbols(pattern.query, max_results=max_candidates_per_adapter)
            if not _same_global_generation(state.generations, batch.generations):
                return _generation_not_ready(workspace, state, batch.generations)
            candidates = _candidate_files(
                batch.raw_candidates,
                pattern,
                substring_matching=substring_matching,
                workspace=workspace,
                scope=state.configured_program,
            )
            if len(candidates) > max_candidates_per_adapter:
                omitted += len(candidates) - max_candidates_per_adapter
                candidates = candidates[:max_candidates_per_adapter]
            omitted += batch.omitted_count
            grouped: dict[tuple[str, str], list[_Candidate]] = defaultdict(list)
            for candidate in candidates:
                grouped[(candidate.relative_path, candidate.uri)].append(candidate)
            for (relative_path, uri), file_candidates in sorted(grouped.items()):
                document = provider.document_symbols(relative_path, uri)
                if (
                    document.relative_path != relative_path
                    or document.uri != uri
                    or not _same_global_generation(state.generations, document.generations)
                ):
                    return _generation_not_ready(workspace, state, document.generations)
                roots = normalize_document_symbols(
                    document.raw_symbols,
                    document_uri=document.uri,
                    normalize_name=document.normalize_name,
                    recover_containment=document.recover_containment,
                    body_completeness=document.body_completeness,
                )
                for symbol in (symbol for root in roots for symbol in root.iter_depth_first()):
                    if not _matches_name_path(symbol, pattern, substring_matching):
                        continue
                    if not any(_candidate_verifies_symbol(candidate, symbol) for candidate in file_candidates):
                        continue
                    if include_body and symbol.body_incomplete_reason is not None:
                        return error(
                            ErrorCode.UNSUPPORTED,
                            details={
                                "operation": "find_symbol",
                                "reason": "incomplete_assignment_range",
                                "recovery_reason": symbol.body_incomplete_reason,
                                "relative_path": relative_path,
                                "name_path": "/".join(symbol.name_path),
                            },
                            workspace=workspace,
                            adapter=state.adapter,
                            generations=state.generations,
                        )
                    try:
                        rendered.append(
                            _symbol_data(
                                state,
                                document,
                                symbol,
                                include_body=include_body,
                                include_info=include_info,
                            )
                        )
                    except PositionError:
                        return error(
                            ErrorCode.NOT_READY,
                            retry=RetryMetadata(retryable=True),
                            details={
                                "reason": "candidate_snapshot_range_mismatch",
                                "relative_path": relative_path,
                            },
                            workspace=workspace,
                        )
            current = provider.global_symbol_state()
            if (
                current.workspace != state.workspace
                or current.adapter != state.adapter
                or not _same_configured_scope(current.configured_program, state.configured_program)
                or not current.workspace_symbols_supported
                or not current.global_ready
                or not _same_global_generation(state.generations, current.generations)
            ):
                return _generation_not_ready(workspace, state, current.generations)

        rendered = _deduplicate_and_sort(rendered)
        scope_data = [_state_data(state) for state in states]
        base: dict[str, Any] = {
            "name_path": pattern.expression,
            "substring_matching": substring_matching,
            "include_body": include_body,
            "include_info": include_info,
            "scope": "configured_program",
            "adapters": scope_data,
            "symbols": [],
        }
        if not rendered and omitted == 0:
            return error(
                ErrorCode.SYMBOL_NOT_FOUND,
                details={
                    "name_path": pattern.expression,
                    "scope": "configured_program",
                    "adapters": scope_data,
                },
                workspace=workspace,
            )
        if len(_canonical_json(base)) > max_answer_chars:
            return error(
                ErrorCode.INVALID_INPUT,
                details={"field": "max_answer_chars", "minimum_required": len(_canonical_json(base))},
                workspace=workspace,
            )
        kept: list[dict[str, Any]] = []
        for symbol in rendered:
            candidate_data = {**base, "symbols": [*kept, symbol]}
            if len(_canonical_json(candidate_data)) > max_answer_chars:
                break
            kept.append(symbol)
        omitted += len(rendered) - len(kept)
        data: Any = {**base, "symbols": kept}
        single = states[0] if len(states) == 1 else None
        return success(
            cast(JsonValue, data),
            workspace=workspace,
            adapter=single.adapter if single is not None else None,
            generations=single.generations if single is not None else None,
            truncation=TruncationMetadata(omitted > 0, omitted),
        )


def _candidate_files(
    raw_candidates: Sequence[RawSymbol],
    pattern: _NamePathPattern,
    *,
    substring_matching: bool,
    workspace: WorkspaceMetadata,
    scope: ConfiguredProgramScope,
) -> list[_Candidate]:
    accepted: list[_Candidate] = []
    allowed = frozenset(scope.paths)
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        kind = raw.get("kind")
        location = raw.get("location")
        if (
            not isinstance(name, str)
            or not name
            or isinstance(kind, bool)
            or not isinstance(kind, int)
            or not isinstance(location, Mapping)
        ):
            continue
        if not (pattern.query in name if substring_matching else pattern.query == name):
            continue
        uri = location.get("uri")
        if not isinstance(uri, str):
            continue
        relative_path = _workspace_relative_path(uri, workspace.root)
        if relative_path is None or relative_path not in allowed:
            continue
        raw_range = location.get("range")
        try:
            candidate_range = _range(raw_range) if isinstance(raw_range, Mapping) else None
        except (TypeError, ValueError):
            continue
        accepted.append(_Candidate(name, kind, relative_path, uri, candidate_range))
    accepted.sort(key=_candidate_key)
    return accepted


def _candidate_verifies_symbol(candidate: _Candidate, symbol: NormalizedSymbol) -> bool:
    if candidate.name != symbol.name or candidate.kind != symbol.kind or candidate.uri != symbol.location.uri:
        return False
    if candidate.range is None:
        return True
    start = candidate.range.start
    return symbol.location.range.start <= start < symbol.location.range.end or start == symbol.location.range.start


def _matches_name_path(
    symbol: NormalizedSymbol,
    pattern: _NamePathPattern,
    substring_matching: bool,
) -> bool:
    components = symbol.name_path
    if len(components) < len(pattern.components):
        return False
    if pattern.absolute and len(components) != len(pattern.components):
        return False
    suffix = components[-len(pattern.components) :]
    last = len(pattern.components) - 1
    return all(
        expected in actual if substring_matching and index == last else expected == actual
        for index, (expected, actual) in enumerate(zip(pattern.components, suffix, strict=True))
    )


def _symbol_data(
    state: GlobalAdapterState,
    document: DocumentSymbolBatch,
    symbol: NormalizedSymbol,
    *,
    include_body: bool,
    include_info: bool,
) -> dict[str, Any]:
    renderer = PublicPositionRenderer.from_snapshot(document.snapshot, document.position_encoding)
    data: dict[str, Any] = {
        "name": symbol.name,
        "name_path": "/".join(symbol.name_path),
        "kind": symbol.kind,
        "relative_path": document.relative_path,
        "location": {
            "uri": symbol.location.uri,
            "range": renderer.range(
                LspPosition(symbol.location.range.start.line, symbol.location.range.start.character),
                LspPosition(symbol.location.range.end.line, symbol.location.range.end.character),
            ),
        },
        "adapter": state.adapter.to_dict(),
        "generations": state.generations.to_dict(),
    }
    if include_info:
        data["info"] = {
            "detail": symbol.detail,
            "selection_range": renderer.range(
                LspPosition(symbol.selection_range.start.line, symbol.selection_range.start.character),
                LspPosition(symbol.selection_range.end.line, symbol.selection_range.end.character),
            ),
        }
    if include_body:
        data["body"] = renderer.text(
            LspPosition(symbol.location.range.start.line, symbol.location.range.start.character),
            LspPosition(symbol.location.range.end.line, symbol.location.range.end.character),
        )
        data["has_children"] = bool(symbol.children)
        data["sha256"] = hashlib.sha256(document.snapshot.raw_bytes).hexdigest()
    return data


def _state_data(state: GlobalAdapterState) -> dict[str, Any]:
    return {
        "adapter": state.adapter.to_dict(),
        "phase": state.phase,
        "workspace_symbols_supported": state.workspace_symbols_supported,
        "global_ready": state.global_ready,
        "generations": state.generations.to_dict(),
        "configured_program": state.configured_program.to_dict(),
    }


def _generation_not_ready(
    workspace: WorkspaceMetadata,
    state: GlobalAdapterState,
    observed: GenerationMetadata,
) -> ToolEnvelope:
    return error(
        ErrorCode.NOT_READY,
        retry=RetryMetadata(
            retryable=True,
            retry_after_seconds=state.retry_after_seconds,
            target_generation=state.generations.program,
            observed_generation=observed.program,
        ),
        details={
            "scope": "configured_program",
            "adapter": state.adapter.to_dict(),
            "expected_generations": state.generations.to_dict(),
            "observed_generations": observed.to_dict(),
        },
        workspace=workspace,
        adapter=state.adapter,
        generations=state.generations,
    )


def _same_global_generation(expected: GenerationMetadata, observed: GenerationMetadata) -> bool:
    return (
        expected.trust == observed.trust
        and expected.program == observed.program
        and expected.index == observed.index
        and observed.scope in {None, "configured_program"}
    )


def _same_configured_scope(expected: ConfiguredProgramScope, observed: ConfiguredProgramScope) -> bool:
    """Compare constant-size advertised scope facts, never enumerate files."""

    return (
        expected.project_kind == observed.project_kind
        and expected.selected_config_path == observed.selected_config_path
        and expected.file_count == observed.file_count
        and expected.sha256 == observed.sha256
    )


def _common_workspace(states: Sequence[GlobalAdapterState]) -> WorkspaceMetadata | None:
    first = states[0].workspace
    return first if all(state.workspace == first for state in states) else None


def _parse_name_path(value: str | Sequence[str]) -> _NamePathPattern | None:
    if isinstance(value, str):
        absolute = value.startswith("/")
        parts = tuple(value.lstrip("/").rstrip("/").split("/"))
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        absolute = False
        parts = tuple(value)
    else:
        return None
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        return None
    return _NamePathPattern(parts, absolute)


def _workspace_relative_path(uri: str, workspace_root: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    path = PurePosixPath(unquote(parsed.path))
    root = PurePosixPath(workspace_root)
    if ".." in path.parts:
        return None
    try:
        return _relative_path(str(path.relative_to(root)))
    except ValueError:
        return None


def _relative_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError(f"path is not a normalized relative path: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized in {"", "."} or ".." in PurePosixPath(normalized).parts or normalized != path:
        raise ValueError(f"path is not a normalized relative path: {path!r}")
    return normalized


def _range(raw: Mapping[str, Any]) -> Range:
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValueError("range requires start and end")
    return Range(_position(start), _position(end))


def _position(raw: Mapping[str, Any]) -> Position:
    line = raw.get("line")
    character = raw.get("character")
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or isinstance(character, bool)
        or not isinstance(character, int)
    ):
        raise TypeError("position requires integer line and character")
    return Position(line, character)


def _range_data(value: Range) -> dict[str, dict[str, int]]:
    return {
        "start": {"line": value.start.line, "character": value.start.character},
        "end": {"line": value.end.line, "character": value.end.character},
    }


def _adapter_key(state: GlobalAdapterState) -> tuple[str, str]:
    return (state.adapter.name, state.adapter.language or "")


def _candidate_key(candidate: _Candidate) -> tuple[Any, ...]:
    start = candidate.range.start if candidate.range is not None else Position(-1, -1)
    return (candidate.relative_path, candidate.uri, start.line, start.character, candidate.kind, candidate.name)


def _deduplicate_and_sort(symbols: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {_canonical_json(symbol): symbol for symbol in symbols}
    return sorted(
        unique.values(),
        key=lambda symbol: (
            symbol["adapter"]["name"],
            symbol["relative_path"],
            symbol["location"]["range"]["start"]["line"],
            symbol["location"]["range"]["start"]["text_offset"],
            symbol["name_path"],
        ),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
