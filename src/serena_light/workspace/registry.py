"""Thread-safe session bindings for shared workspace runtimes.

Identity resolution belongs to the workspace-identity layer.  This module only
owns the post-resolution state: one warm runtime per identity, independently
issued lease IDs, and a session-to-binding map.  In particular, it deliberately
does not retain a daemon-global "active workspace".
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace[IdentityT]:
    """Identity-layer output needed to bind one activated path.

    ``working_subdirectory`` is intentionally supplied by the identity resolver:
    Git and exact non-Git identities have different normalization rules.
    """

    identity: IdentityT
    working_subdirectory: Path


@dataclass(frozen=True, slots=True)
class WorkspaceLease[IdentityT, RuntimeT]:
    """A daemon-issued lifetime authority for one shared runtime."""

    id: UUID
    identity: IdentityT
    runtime: RuntimeT


@dataclass(frozen=True, slots=True)
class WorkspaceBinding[IdentityT, RuntimeT]:
    """The workspace selected by a session-owned lease."""

    lease: WorkspaceLease[IdentityT, RuntimeT]
    working_subdirectory: Path

    @property
    def identity(self) -> IdentityT:
        return self.lease.identity

    @property
    def runtime(self) -> RuntimeT:
        return self.lease.runtime


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceActivation[IdentityT, RuntimeT, SessionT]:
    """A registry-owned candidate that is not yet visible as a binding."""

    session: SessionT
    expected_binding: WorkspaceBinding[IdentityT, RuntimeT] | None
    binding: WorkspaceBinding[IdentityT, RuntimeT]
    candidate_is_provisional: bool
    candidate_runtime_created: bool


@dataclass(frozen=True, slots=True)
class RuntimeState[RuntimeT]:
    """Observable lifecycle facts for a retained runtime.

    An idle runtime remains retained so the later lease reaper can apply its warm
    grace policy.  This primitive does not schedule that reaper itself.
    """

    runtime: RuntimeT
    reference_count: int
    idle_eligible: bool


@dataclass(slots=True)
class _RuntimeEntry[RuntimeT]:
    runtime: RuntimeT
    reference_count: int = 0


class WorkspaceRuntimeRegistry[IdentityT: Hashable, RuntimeT, SessionT: Hashable]:
    """Own runtime reuse, lease lifetimes, and session-scoped bindings.

    ``activate`` has acquire-then-swap semantics.  The resolver runs before any
    registry mutation, then a new cross-root runtime/lease is acquired before
    replacing the session binding and releasing its old lease.  Thus failures
    leave the old binding and its reference count untouched.
    """

    def __init__(self, runtime_factory: Callable[[IdentityT], RuntimeT]) -> None:
        self._runtime_factory = runtime_factory
        self._lock = RLock()
        self._runtimes: dict[IdentityT, _RuntimeEntry[RuntimeT]] = {}
        self._leases: dict[UUID, WorkspaceLease[IdentityT, RuntimeT]] = {}
        self._bindings: dict[SessionT, WorkspaceBinding[IdentityT, RuntimeT]] = {}

    def activate_path(
        self,
        session: SessionT,
        activation_path: Path,
        resolver: Callable[[Path], ResolvedWorkspace[IdentityT]],
    ) -> WorkspaceBinding[IdentityT, RuntimeT]:
        """Resolve a path first, so resolver failures cannot disturb a binding."""

        return self.activate(session, resolver(activation_path))

    def activate(
        self,
        session: SessionT,
        resolved: ResolvedWorkspace[IdentityT],
    ) -> WorkspaceBinding[IdentityT, RuntimeT]:
        """Bind ``session`` using the supplied normalized workspace result."""

        with self._lock:
            old_binding = self._bindings.get(session)
            if old_binding is not None and old_binding.identity == resolved.identity:
                binding = WorkspaceBinding(old_binding.lease, resolved.working_subdirectory)
                self._bindings[session] = binding
                return binding

            # Construction and lease allocation precede every old-binding mutation.
            new_lease = self._acquire_locked(resolved.identity)
            new_binding = WorkspaceBinding(new_lease, resolved.working_subdirectory)
            self._bindings[session] = new_binding
            if old_binding is not None:
                self._release_locked(old_binding.lease.id)
            return new_binding

    def prepare_activation(
        self,
        session: SessionT,
        resolved: ResolvedWorkspace[IdentityT],
    ) -> PreparedWorkspaceActivation[IdentityT, RuntimeT, SessionT]:
        """Acquire a refresh candidate without replacing the current binding.

        A cross-root candidate owns one temporary registry lease until callers
        commit or abort it. Same-root preparation keeps the existing lease and
        changes no registry state, letting callers validate the shared runtime
        before they make even a working-subdirectory update visible.
        """

        with self._lock:
            expected = self._bindings.get(session)
            if expected is not None and expected.identity == resolved.identity:
                return PreparedWorkspaceActivation(
                    session=session,
                    expected_binding=expected,
                    binding=WorkspaceBinding(expected.lease, resolved.working_subdirectory),
                    candidate_is_provisional=False,
                    candidate_runtime_created=False,
                )
            candidate_runtime_created = resolved.identity not in self._runtimes
            lease = self._acquire_locked(resolved.identity)
            return PreparedWorkspaceActivation(
                session=session,
                expected_binding=expected,
                binding=WorkspaceBinding(lease, resolved.working_subdirectory),
                candidate_is_provisional=True,
                candidate_runtime_created=candidate_runtime_created,
            )

    def commit_activation(
        self,
        prepared: PreparedWorkspaceActivation[IdentityT, RuntimeT, SessionT],
    ) -> WorkspaceBinding[IdentityT, RuntimeT]:
        """Atomically publish a prepared binding only if its prior state remains current."""

        with self._lock:
            current = self._bindings.get(prepared.session)
            if current != prepared.expected_binding:
                self._abort_prepared_locked(prepared)
                raise RuntimeError("workspace binding changed before activation commit")
            if prepared.candidate_is_provisional:
                lease = self._leases.get(prepared.binding.lease.id)
                if lease != prepared.binding.lease:
                    self._abort_prepared_locked(prepared)
                    raise RuntimeError("prepared workspace lease is no longer active")
            self._bindings[prepared.session] = prepared.binding
            if prepared.expected_binding is not None and prepared.candidate_is_provisional:
                self._release_locked(prepared.expected_binding.lease.id)
            return prepared.binding

    def abort_activation(self, prepared: PreparedWorkspaceActivation[IdentityT, RuntimeT, SessionT]) -> bool:
        """Discard only a cross-root candidate; never change the current binding."""

        with self._lock:
            return self._abort_prepared_locked(prepared)

    def binding_for(self, session: SessionT) -> WorkspaceBinding[IdentityT, RuntimeT] | None:
        """Return the current session binding, if one is active."""

        with self._lock:
            return self._bindings.get(session)

    def release(self, session: SessionT) -> bool:
        """Release a session binding once; repeated release is harmless."""

        with self._lock:
            binding = self._bindings.pop(session, None)
            return binding is not None and self._release_locked(binding.lease.id)

    def release_lease(self, lease_id: UUID) -> bool:
        """Release an active lease once, detaching any matching session binding."""

        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            for session, binding in tuple(self._bindings.items()):
                if binding.lease.id == lease_id:
                    del self._bindings[session]
            return self._release_locked(lease.id)

    def runtime_state(self, identity: IdentityT) -> RuntimeState[RuntimeT] | None:
        """Return reference and idle eligibility without exposing mutable entries."""

        with self._lock:
            entry = self._runtimes.get(identity)
            if entry is None:
                return None
            return RuntimeState(
                runtime=entry.runtime,
                reference_count=entry.reference_count,
                idle_eligible=entry.reference_count == 0,
            )

    def retire_idle(self, identity: IdentityT, expected_runtime: RuntimeT) -> RuntimeT | None:
        """Detach the expected runtime atomically only while it is unleased.

        Warm-grace scheduling belongs to the daemon lease lifecycle. Once that
        policy decides an idle runtime should stop, this compare-and-remove
        operation closes the race with concurrent activation. The detached
        runtime can then be stopped outside the registry lock.
        """

        with self._lock:
            entry = self._runtimes.get(identity)
            if entry is None or entry.runtime is not expected_runtime or entry.reference_count != 0:
                return None
            del self._runtimes[identity]
            return entry.runtime

    def _acquire_locked(self, identity: IdentityT) -> WorkspaceLease[IdentityT, RuntimeT]:
        entry = self._runtimes.get(identity)
        if entry is None:
            entry = _RuntimeEntry(runtime=self._runtime_factory(identity))
            self._runtimes[identity] = entry
        entry.reference_count += 1
        lease = WorkspaceLease(id=uuid4(), identity=identity, runtime=entry.runtime)
        self._leases[lease.id] = lease
        return lease

    def _release_locked(self, lease_id: UUID) -> bool:
        lease = self._leases.pop(lease_id, None)
        if lease is None:
            return False
        entry = self._runtimes[lease.identity]
        entry.reference_count -= 1
        if entry.reference_count < 0:  # pragma: no cover - internal invariant
            raise RuntimeError(f"workspace lease underflow for {lease.identity!r}")
        return True

    def _abort_prepared_locked(
        self,
        prepared: PreparedWorkspaceActivation[IdentityT, RuntimeT, SessionT],
    ) -> bool:
        if not prepared.candidate_is_provisional:
            return False
        return self._release_locked(prepared.binding.lease.id)
