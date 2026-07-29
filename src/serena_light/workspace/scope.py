"""Native-program scope projection and generation-readiness state.

Adapters own native configuration discovery.  This module accepts their
normalized file attribution, compares it with the trust inventory, and tracks
which attributed generations have actually been observed by the language
server.  It deliberately does not parse project files or synthesize overlays.
"""

from __future__ import annotations

import hashlib
import posixpath
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import PurePosixPath
from threading import Condition, RLock
from types import MappingProxyType
from typing import TypedDict


class LanguageFamily(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"


class ProjectKind(StrEnum):
    CONFIGURED = "configured"
    WORKSPACE_DEFAULT = "workspace_default"


class DifferenceReason(StrEnum):
    EXCLUDED_BY_NATIVE_CONFIG = "excluded_by_native_config"
    OMITTED_BY_ENGINE_WORKSPACE_PROGRAM = "omitted_by_engine_workspace_program"
    ABSENT_FROM_TRUST_INVENTORY = "absent_from_trust_inventory"
    ABSENT_FROM_GIT_TRUST_INVENTORY = "absent_from_git_trust_inventory"
    GIT_IGNORED = "git_ignored"
    NOT_IN_GIT_INVENTORY = "not_in_git_inventory"
    EXCLUDED_FROM_BOUNDED_INVENTORY = "excluded_from_bounded_inventory"
    MISSING = "missing"
    NON_REGULAR = "non_regular"
    OUTSIDE_WORKSPACE = "outside_workspace"
    SYMLINK_OR_ESCAPE = "symlink_or_escape"


class ScopeCode(StrEnum):
    SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"


class ReadinessCode(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"


class ReadinessScope(StrEnum):
    CONFIGURED_PROGRAM = "configured_program"
    PATH = "path"


class FileChangeType(IntEnum):
    """LSP ``FileChangeType`` wire values."""

    CREATED = 1
    CHANGED = 2
    DELETED = 3


_SUFFIXES: Mapping[LanguageFamily, frozenset[str]] = MappingProxyType(
    {
        LanguageFamily.PYTHON: frozenset({".py", ".pyi"}),
        LanguageFamily.TYPESCRIPT: frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}),
    }
)


def _normalize_path(path: str) -> str:
    if not path or path.startswith("/") or "\x00" in path or "\\" in path:
        raise ValueError(f"path is not a normalized POSIX path: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized in {"", "."} or ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"path is not a normalized attributed path: {path!r}")
    return normalized


def _supported(path: str, language: LanguageFamily) -> bool:
    return PurePosixPath(path).suffix.lower() in _SUFFIXES[language]


def _path_set(paths: Iterable[str], language: LanguageFamily) -> tuple[str, ...]:
    return tuple(sorted({_normalize_path(path) for path in paths if _supported(path, language)}))


def _path_digest(paths: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(paths).encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(frozen=True, slots=True)
class PathSetEvidence:
    paths: tuple[str, ...]
    count: int
    sha256: str

    @classmethod
    def from_paths(cls, paths: tuple[str, ...]) -> PathSetEvidence:
        return cls(paths=paths, count=len(paths), sha256=_path_digest(paths))


@dataclass(frozen=True, slots=True)
class ScopeDifference:
    path: str
    reason: DifferenceReason


@dataclass(frozen=True, slots=True)
class ScopeError:
    code: ScopeCode
    message: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeProgramAttribution:
    """File-level native-program facts supplied by an adapter or probe."""

    language: LanguageFamily
    project_kind: ProjectKind
    selected_config_path: str | None
    configured_program_paths: Iterable[str]

    def __post_init__(self) -> None:
        selected = self.selected_config_path
        if selected is not None:
            object.__setattr__(self, "selected_config_path", _normalize_path(selected))
        if self.project_kind is ProjectKind.CONFIGURED and selected is None:
            raise ValueError("a configured project requires selected_config_path")
        if self.project_kind is ProjectKind.WORKSPACE_DEFAULT and selected is not None:
            raise ValueError("a workspace-default project cannot select a native config")
        object.__setattr__(self, "configured_program_paths", _path_set(self.configured_program_paths, self.language))


@dataclass(frozen=True, slots=True)
class ScopeProjection:
    """Immutable trust/native-program comparison for one language adapter."""

    language: LanguageFamily
    project_kind: ProjectKind
    selected_config_path: str | None
    trust_inventory: PathSetEvidence
    configured_program: PathSetEvidence
    trusted_not_in_configured_program: tuple[ScopeDifference, ...]
    configured_program_outside_trust: tuple[ScopeDifference, ...]
    compatible: bool
    error: ScopeError | None
    overlay_generated: bool = False

    @classmethod
    def from_attribution(
        cls,
        *,
        trust_inventory_paths: Iterable[str],
        attribution: NativeProgramAttribution,
        outside_trust_reasons: Mapping[str, DifferenceReason] | None = None,
    ) -> ScopeProjection:
        trust = _path_set(trust_inventory_paths, attribution.language)
        program = tuple(attribution.configured_program_paths)
        omitted_reason = (
            DifferenceReason.EXCLUDED_BY_NATIVE_CONFIG
            if attribution.project_kind is ProjectKind.CONFIGURED
            else DifferenceReason.OMITTED_BY_ENGINE_WORKSPACE_PROGRAM
        )
        omitted = tuple(ScopeDifference(path, omitted_reason) for path in sorted(set(trust).difference(program)))
        supplied_reasons = {_normalize_path(path): reason for path, reason in (outside_trust_reasons or {}).items()}
        outside = tuple(
            ScopeDifference(path, supplied_reasons.get(path, DifferenceReason.ABSENT_FROM_TRUST_INVENTORY))
            for path in sorted(set(program).difference(trust))
        )
        error = (
            ScopeError(
                code=ScopeCode.SCOPE_INCOMPATIBLE,
                message="configured program contains supported-language paths outside trust",
                paths=tuple(difference.path for difference in outside),
            )
            if outside
            else None
        )
        return cls(
            language=attribution.language,
            project_kind=attribution.project_kind,
            selected_config_path=attribution.selected_config_path,
            trust_inventory=PathSetEvidence.from_paths(trust),
            configured_program=PathSetEvidence.from_paths(program),
            trusted_not_in_configured_program=omitted,
            configured_program_outside_trust=outside,
            compatible=not outside,
            error=error,
        )


MAX_STATUS_DIFFERENCES = 50


class BoundedDifferenceStatus(TypedDict):
    items: tuple[dict[str, str], ...]
    total: int
    digest: str
    omitted_count: int


def bounded_difference_status(
    differences: Iterable[ScopeDifference],
    *,
    limit: int = MAX_STATUS_DIFFERENCES,
) -> BoundedDifferenceStatus:
    """Render deterministic bounded evidence while hashing the complete set."""

    if limit < 0:
        raise ValueError("difference status limit must be non-negative")
    ordered = tuple(sorted(differences, key=lambda item: (item.path, item.reason.value)))
    digest = hashlib.sha256()
    for item in ordered:
        digest.update(item.path.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(item.reason.value.encode("ascii"))
        digest.update(b"\0")
    return {
        "items": tuple({"path": item.path, "reason": item.reason.value} for item in ordered[:limit]),
        "total": len(ordered),
        "digest": digest.hexdigest(),
        "omitted_count": max(0, len(ordered) - limit),
    }


@dataclass(frozen=True, slots=True)
class WatchedFileEvent:
    path: str
    change_type: FileChangeType
    may_change_program: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_path(self.path))

    def as_lsp_change(self, workspace_uri: str) -> Mapping[str, object]:
        base = workspace_uri.rstrip("/")
        return MappingProxyType({"uri": f"{base}/{self.path}", "type": int(self.change_type)})


@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    trust_inventory: int
    configured_program: int
    observed_configured_program: int
    path_scoped: Mapping[str, int]
    observed_path_scoped: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class GenerationTransition:
    events: tuple[WatchedFileEvent, ...]
    trust_inventory_changed: bool
    configured_program_invalidated: bool
    invalidated_paths: tuple[str, ...]
    before: GenerationSnapshot
    after: GenerationSnapshot


@dataclass(frozen=True, slots=True)
class RetryMetadata:
    retryable: bool
    retry_after_seconds: float
    waited_seconds: float
    timeout_seconds: float
    target_generation: int
    observed_generation: int


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    code: ReadinessCode
    scope: ReadinessScope
    ready: bool
    path: str | None
    target_generation: int
    observed_generation: int
    retry: RetryMetadata | None
    scope_error: ScopeError | None = None


_Waiter = Callable[[Condition, float], None]


def _condition_wait(condition: Condition, timeout: float) -> None:
    condition.wait(timeout)


class ScopeGenerationTracker:
    """Thread-safe projection generations and bounded observation barriers."""

    def __init__(
        self,
        projection: ScopeProjection,
        *,
        max_wait_seconds: float = 30.0,
        retry_after_seconds: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        waiter: _Waiter = _condition_wait,
    ) -> None:
        if max_wait_seconds < 0 or retry_after_seconds < 0:
            raise ValueError("wait bounds must be non-negative")
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._projection = projection
        self._max_wait_seconds = max_wait_seconds
        self._retry_after_seconds = retry_after_seconds
        self._clock = clock
        self._waiter = waiter
        self._trust_generation = 0
        # Installing the native configured program is the first material
        # program generation.  Zero remains the sentinel for "no indexed
        # program has been observed" in the LSP state.
        self._program_generation = 1
        self._observed_program_generation = -1
        self._path_generations: dict[str, int] = {}
        self._observed_path_generations: dict[str, int] = {}

    @property
    def projection(self) -> ScopeProjection:
        with self._lock:
            return self._projection

    @property
    def generations(self) -> GenerationSnapshot:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> GenerationSnapshot:
        return GenerationSnapshot(
            trust_inventory=self._trust_generation,
            configured_program=self._program_generation,
            observed_configured_program=self._observed_program_generation,
            path_scoped=MappingProxyType(dict(self._path_generations)),
            observed_path_scoped=MappingProxyType(dict(self._observed_path_generations)),
        )

    def update_projection(self, projection: ScopeProjection) -> GenerationSnapshot:
        """Install fresh adapter attribution, advancing only changed surfaces."""

        with self._condition:
            if projection.language is not self._projection.language:
                raise ValueError("cannot change a generation tracker's language family")
            previous = self._projection
            if projection.trust_inventory != previous.trust_inventory:
                self._trust_generation += 1
            if (
                projection.configured_program != previous.configured_program
                or projection.selected_config_path != previous.selected_config_path
                or projection.project_kind is not previous.project_kind
            ):
                self._program_generation += 1
            self._projection = projection
            self._condition.notify_all()
            return self._snapshot()

    def apply_did_change_watched_files(self, events: Iterable[WatchedFileEvent]) -> GenerationTransition:
        """Apply one watcher batch without changing attributed program membership."""

        batch = tuple(events)
        with self._condition:
            before = self._snapshot()
            trust_paths = set(self._projection.trust_inventory.paths)
            program_paths = set(self._projection.configured_program.paths)
            selected_config = self._projection.selected_config_path
            relevant_sources = tuple(event for event in batch if _supported(event.path, self._projection.language))
            trusted_events = tuple(event for event in relevant_sources if event.path in trust_paths)
            program_invalidated = any(
                event.path in program_paths
                or event.may_change_program
                or (selected_config is not None and event.path == selected_config)
                for event in batch
            )
            if trusted_events:
                self._trust_generation += 1
            if program_invalidated:
                self._program_generation += 1

            invalidated_paths: set[str] = set()
            for event in trusted_events:
                self._path_generations[event.path] = self._path_generations.get(event.path, 0) + 1
                invalidated_paths.add(event.path)

            after = self._snapshot()
            if before != after:
                self._condition.notify_all()
            return GenerationTransition(
                events=batch,
                trust_inventory_changed=bool(trusted_events),
                configured_program_invalidated=program_invalidated,
                invalidated_paths=tuple(sorted(invalidated_paths)),
                before=before,
                after=after,
            )

    def observe_configured_program(self, generation: int) -> bool:
        with self._condition:
            if generation < 0 or generation > self._program_generation:
                return False
            if generation > self._observed_program_generation:
                self._observed_program_generation = generation
                self._condition.notify_all()
            return generation == self._program_generation

    def observe_path(self, path: str, generation: int) -> bool:
        normalized = _normalize_path(path)
        with self._condition:
            current = self._path_generations.get(normalized, 0)
            if generation < 0 or generation > current:
                return False
            previous = self._observed_path_generations.get(normalized, -1)
            if generation > previous:
                self._observed_path_generations[normalized] = generation
                self._condition.notify_all()
            return generation == current

    def wait_for_configured_program(self, timeout: float | None = None) -> ReadinessResult:
        return self._wait(ReadinessScope.CONFIGURED_PROGRAM, None, timeout)

    def wait_for_path(self, path: str, timeout: float | None = None) -> ReadinessResult:
        normalized = _normalize_path(path)
        with self._lock:
            if normalized not in self._projection.trust_inventory.paths:
                raise ValueError(f"path is outside the current trust inventory: {normalized}")
        return self._wait(ReadinessScope.PATH, normalized, timeout)

    def _wait(self, scope: ReadinessScope, path: str | None, timeout: float | None) -> ReadinessResult:
        requested = self._max_wait_seconds if timeout is None else timeout
        if requested < 0:
            raise ValueError("timeout must be non-negative")
        bounded = min(requested, self._max_wait_seconds)
        started = self._clock()
        deadline = started + bounded

        with self._condition:
            while True:
                if scope is ReadinessScope.CONFIGURED_PROGRAM and not self._projection.compatible:
                    return ReadinessResult(
                        code=ReadinessCode.SCOPE_INCOMPATIBLE,
                        scope=scope,
                        ready=False,
                        path=None,
                        target_generation=self._program_generation,
                        observed_generation=self._observed_program_generation,
                        retry=None,
                        scope_error=self._projection.error,
                    )
                target, observed = self._target_and_observed(scope, path)
                if observed >= target:
                    return ReadinessResult(
                        code=ReadinessCode.READY,
                        scope=scope,
                        ready=True,
                        path=path,
                        target_generation=target,
                        observed_generation=observed,
                        retry=None,
                    )
                remaining = deadline - self._clock()
                if remaining <= 0:
                    waited = max(0.0, self._clock() - started)
                    return ReadinessResult(
                        code=ReadinessCode.NOT_READY,
                        scope=scope,
                        ready=False,
                        path=path,
                        target_generation=target,
                        observed_generation=observed,
                        retry=RetryMetadata(
                            retryable=True,
                            retry_after_seconds=self._retry_after_seconds,
                            waited_seconds=waited,
                            timeout_seconds=bounded,
                            target_generation=target,
                            observed_generation=observed,
                        ),
                    )
                self._waiter(self._condition, remaining)

    def _target_and_observed(self, scope: ReadinessScope, path: str | None) -> tuple[int, int]:
        if scope is ReadinessScope.CONFIGURED_PROGRAM:
            return self._program_generation, self._observed_program_generation
        assert path is not None
        return self._path_generations.get(path, 0), self._observed_path_generations.get(path, -1)
