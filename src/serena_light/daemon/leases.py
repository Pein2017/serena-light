"""Daemon-issued, lease-scoped workspace lifetime state.

This is deliberately a synchronous, thread-safe model rather than an async
task.  The daemon owns when to call :meth:`LeaseLifecycle.sweep`; callers apply
the returned decisions to the workspace registry and runtime.  In particular,
an HTTP/MCP session is never a lifetime owner here.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from uuid import UUID, uuid4

from serena_light.workspace.registry import WorkspaceBinding

HEARTBEAT_INTERVAL_SECONDS = 15.0
LEASE_EXPIRY_SECONDS = 60.0
WARM_GRACE_SECONDS = 10 * 60.0


class LeaseErrorCode(StrEnum):
    """Transport-neutral lifecycle failures owned by this module."""

    LEASE_EXPIRED = "LEASE_EXPIRED"


class LeaseError[IdentityT, RuntimeT](RuntimeError):
    """Raised when a caller tries to use a lease that is no longer active."""

    def __init__(
        self,
        code: LeaseErrorCode,
        message: str,
        *,
        decision: LeaseLifecycleDecision[IdentityT, RuntimeT] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.decision = decision


class LeaseExpiredError[IdentityT, RuntimeT](LeaseError[IdentityT, RuntimeT]):
    """A lease reached its exact expiry boundary and cannot be recreated."""

    def __init__(self, lease_id: UUID, decision: LeaseLifecycleDecision[IdentityT, RuntimeT] | None) -> None:
        super().__init__(
            LeaseErrorCode.LEASE_EXPIRED,
            f"lease {lease_id} has expired",
            decision=decision,
        )


@dataclass(frozen=True, slots=True)
class DaemonLease[IdentityT, RuntimeT]:
    """The only daemon lifetime authority for one workspace binding."""

    id: UUID
    binding: WorkspaceBinding[IdentityT, RuntimeT] | None
    issued_at: float
    last_heartbeat_at: float
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS
    expiry_seconds: float = LEASE_EXPIRY_SECONDS

    @property
    def identity(self) -> IdentityT | None:
        return None if self.binding is None else self.binding.identity

    @property
    def heartbeat_due_at(self) -> float:
        return self.last_heartbeat_at + self.heartbeat_interval_seconds

    @property
    def expires_at(self) -> float:
        return self.last_heartbeat_at + self.expiry_seconds


class LeaseEndReason(StrEnum):
    RELEASED = "released"
    EXPIRED = "expired"
    GRACE_EXPIRED = "grace_expired"


@dataclass(frozen=True, slots=True)
class LeaseLifecycleDecision[IdentityT, RuntimeT]:
    """An effect for the daemon, never an effect performed by this model.

    ``binding_to_release`` is supplied for every released or expired lease so
    the daemon can detach the corresponding registry binding.  A non-``None``
    ``runtime_to_stop`` is the sole signal to stop a workspace runtime.
    """

    reason: LeaseEndReason
    identity: IdentityT | None
    active_holders: int
    binding_to_release: WorkspaceBinding[IdentityT, RuntimeT] | None = None
    lease_id: UUID | None = None
    runtime_to_stop: RuntimeT | None = None
    grace_deadline: float | None = None


@dataclass(frozen=True, slots=True)
class ReleaseResult[IdentityT, RuntimeT]:
    """Result of an idempotent release request."""

    released: bool
    decision: LeaseLifecycleDecision[IdentityT, RuntimeT] | None = None


@dataclass(slots=True)
class _WorkspaceState[RuntimeT]:
    runtime: RuntimeT
    holders: set[UUID]
    grace_deadline: float | None = None


class LeaseLifecycle[IdentityT: Hashable, RuntimeT]:
    """Track daemon leases independently from blocking LSP work.

    The caller injects a monotonic clock in production and a fake clock in
    tests.  ``issue`` is called only after registry activation has yielded a
    :class:`WorkspaceBinding`; the returned UUID, not that registry's session
    bookkeeping, is then the authority for every daemon request.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        expiry_seconds: float = LEASE_EXPIRY_SECONDS,
        warm_grace_seconds: float = WARM_GRACE_SECONDS,
    ) -> None:
        if heartbeat_interval_seconds <= 0 or expiry_seconds <= 0 or warm_grace_seconds < 0:
            raise ValueError("lease intervals must be positive (grace may be zero)")
        if heartbeat_interval_seconds > expiry_seconds:
            raise ValueError("heartbeat cadence cannot exceed lease expiry")
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._expiry_seconds = expiry_seconds
        self._warm_grace_seconds = warm_grace_seconds
        self._lock = RLock()
        self._active: dict[UUID, DaemonLease[IdentityT, RuntimeT]] = {}
        self._expired: set[UUID] = set()
        self._workspaces: dict[IdentityT, _WorkspaceState[RuntimeT]] = {}

    def issue(
        self,
        binding: WorkspaceBinding[IdentityT, RuntimeT] | None = None,
    ) -> DaemonLease[IdentityT, RuntimeT]:
        """Issue one distinct daemon lease, optionally before workspace binding."""

        with self._lock:
            now = self._clock()
            lease = DaemonLease(
                id=uuid4(),
                binding=binding,
                issued_at=now,
                last_heartbeat_at=now,
                heartbeat_interval_seconds=self._heartbeat_interval_seconds,
                expiry_seconds=self._expiry_seconds,
            )
            self._active[lease.id] = lease
            if binding is not None:
                self._add_holder_locked(lease.id, binding)
            return lease

    def acquire_lease(
        self,
        binding: WorkspaceBinding[IdentityT, RuntimeT] | None = None,
    ) -> DaemonLease[IdentityT, RuntimeT]:
        """Server-facing name for issuing authority before optional activation."""

        return self.issue(binding)

    def rebind(
        self,
        lease_id: UUID,
        binding: WorkspaceBinding[IdentityT, RuntimeT],
    ) -> DaemonLease[IdentityT, RuntimeT]:
        """Attach or atomically move one live daemon lease to ``binding``.

        The registry performs acquire-then-swap first. The daemon service must
        serialize that registry mutation with this method so a concurrent
        release cannot invalidate the authority between the two operations.
        """

        with self._lock:
            now = self._clock()
            lease = self._require_active_locked(lease_id, now)
            old_binding = lease.binding
            if old_binding is not None and old_binding.identity != binding.identity:
                self._remove_holder_locked(lease.id, old_binding.identity, now)
            if old_binding is None or old_binding.identity != binding.identity:
                self._add_holder_locked(lease.id, binding)
            elif old_binding.runtime is not binding.runtime:
                raise ValueError("one workspace identity cannot change runtime while leased")
            rebound = DaemonLease(
                id=lease.id,
                binding=binding,
                issued_at=lease.issued_at,
                last_heartbeat_at=lease.last_heartbeat_at,
                heartbeat_interval_seconds=lease.heartbeat_interval_seconds,
                expiry_seconds=lease.expiry_seconds,
            )
            self._active[lease_id] = rebound
            return rebound

    def heartbeat(self, lease_id: UUID) -> DaemonLease[IdentityT, RuntimeT]:
        """Renew a live lease without depending on workspace executor progress."""

        with self._lock:
            now = self._clock()
            lease = self._require_active_locked(lease_id, now)
            renewed = DaemonLease(
                id=lease.id,
                binding=lease.binding,
                issued_at=lease.issued_at,
                last_heartbeat_at=now,
                heartbeat_interval_seconds=lease.heartbeat_interval_seconds,
                expiry_seconds=lease.expiry_seconds,
            )
            self._active[lease_id] = renewed
            return renewed

    def require_active(self, lease_id: UUID) -> DaemonLease[IdentityT, RuntimeT]:
        """Return a live lease or raise typed ``LEASE_EXPIRED`` at the boundary."""

        with self._lock:
            return self._require_active_locked(lease_id, self._clock())

    def binding_for(self, lease_id: UUID) -> WorkspaceBinding[IdentityT, RuntimeT]:
        """Resolve a binding only through an active daemon lease."""

        binding = self.require_active(lease_id).binding
        if binding is None:
            raise ValueError("lease has no active workspace binding")
        return binding

    def release(self, lease_id: UUID, *, immediate: bool = False) -> ReleaseResult[IdentityT, RuntimeT]:
        """Release normally and idempotently; immediate stops only the last holder."""

        with self._lock:
            lease = self._active.get(lease_id)
            if lease is None:
                return ReleaseResult(released=False)
            now = self._clock()
            if now >= lease.expires_at:
                decision = self._end_locked(lease, LeaseEndReason.EXPIRED, now, immediate=False)
                self._expired.add(lease_id)
                return ReleaseResult(released=False, decision=decision)
            return ReleaseResult(
                released=True,
                decision=self._end_locked(lease, LeaseEndReason.RELEASED, now, immediate=immediate),
            )

    def release_lease(self, lease_id: UUID, *, immediate: bool = False) -> ReleaseResult[IdentityT, RuntimeT]:
        """Server-facing idempotent release with registry/runtime decisions."""

        return self.release(lease_id, immediate=immediate)

    def release_workspace(
        self,
        lease_id: UUID,
        *,
        immediate: bool = False,
    ) -> ReleaseResult[IdentityT, RuntimeT]:
        """Detach one binding while retaining its live daemon lease.

        This is the public counterpart of ``release_lease`` for an MCP client
        that wants to keep heartbeating and activate a different workspace
        later. Immediate release stops the runtime only when this was its last
        holder; otherwise the remaining holders continue serving it.
        """

        with self._lock:
            now = self._clock()
            lease = self._require_active_locked(lease_id, now)
            binding = lease.binding
            if binding is None:
                return ReleaseResult(released=False)
            decision = self._remove_holder_locked(
                lease.id,
                binding.identity,
                now,
                immediate=immediate,
            )
            self._active[lease.id] = DaemonLease(
                id=lease.id,
                binding=None,
                issued_at=lease.issued_at,
                last_heartbeat_at=lease.last_heartbeat_at,
                heartbeat_interval_seconds=lease.heartbeat_interval_seconds,
                expiry_seconds=lease.expiry_seconds,
            )
            return ReleaseResult(
                released=True,
                decision=LeaseLifecycleDecision(
                    reason=LeaseEndReason.RELEASED,
                    identity=binding.identity,
                    active_holders=decision.active_holders,
                    binding_to_release=binding,
                    lease_id=lease.id,
                    runtime_to_stop=decision.runtime_to_stop,
                    grace_deadline=decision.grace_deadline,
                ),
            )

    def sweep(self) -> tuple[LeaseLifecycleDecision[IdentityT, RuntimeT], ...]:
        """Expire overdue leases and emit stop decisions for elapsed warm grace."""

        with self._lock:
            now = self._clock()
            decisions: list[LeaseLifecycleDecision[IdentityT, RuntimeT]] = []
            for lease in tuple(self._active.values()):
                if now >= lease.expires_at:
                    decisions.append(self._end_locked(lease, LeaseEndReason.EXPIRED, now, immediate=False))
                    self._expired.add(lease.id)
            for identity, state in tuple(self._workspaces.items()):
                if state.holders or state.grace_deadline is None or now < state.grace_deadline:
                    continue
                decisions.append(
                    LeaseLifecycleDecision(
                        reason=LeaseEndReason.GRACE_EXPIRED,
                        identity=identity,
                        active_holders=0,
                        runtime_to_stop=state.runtime,
                    )
                )
                del self._workspaces[identity]
            return tuple(decisions)

    def active_holders(self, identity: IdentityT) -> int:
        """Return the current holder count for one physical workspace identity."""

        with self._lock:
            state = self._workspaces.get(identity)
            return 0 if state is None else len(state.holders)

    def grace_deadline(self, identity: IdentityT) -> float | None:
        """Expose a grace deadline for status without exposing mutable state."""

        with self._lock:
            state = self._workspaces.get(identity)
            return None if state is None else state.grace_deadline

    def daemon_idle(self) -> bool:
        """Return true only when no lease or warm workspace still owns this daemon."""

        with self._lock:
            return not self._active and not self._workspaces

    def active_lease_count(self) -> int:
        """Return the holder count used by authenticated legacy retirement."""

        with self._lock:
            return len(self._active)

    def _require_active_locked(self, lease_id: UUID, now: float) -> DaemonLease[IdentityT, RuntimeT]:
        lease = self._active.get(lease_id)
        if lease is not None:
            if now < lease.expires_at:
                return lease
            decision = self._end_locked(lease, LeaseEndReason.EXPIRED, now, immediate=False)
            self._expired.add(lease_id)
            raise LeaseExpiredError(lease_id, decision)
        if lease_id in self._expired:
            # The initial expiry decision carried the binding-release effect.
            # Repeated use is still typed but must not ask the daemon to release it twice.
            raise LeaseExpiredError(lease_id, None)
        # Do not expose whether a UUID was once valid. Missing daemon-lifetime
        # authority has the same public meaning as an expired lease.
        raise LeaseExpiredError(lease_id, None)

    def _end_locked(
        self,
        lease: DaemonLease[IdentityT, RuntimeT],
        reason: LeaseEndReason,
        now: float,
        *,
        immediate: bool,
    ) -> LeaseLifecycleDecision[IdentityT, RuntimeT]:
        del self._active[lease.id]
        binding = lease.binding
        if binding is None:
            return LeaseLifecycleDecision(
                reason=reason,
                identity=None,
                active_holders=0,
                lease_id=lease.id,
            )
        state = self._workspaces[binding.identity]
        state.holders.remove(lease.id)
        active_holders = len(state.holders)
        runtime_to_stop: RuntimeT | None = None
        grace_deadline: float | None = None
        if active_holders == 0:
            if immediate:
                runtime_to_stop = state.runtime
                del self._workspaces[binding.identity]
            else:
                grace_deadline = now + self._warm_grace_seconds
                state.grace_deadline = grace_deadline
        return LeaseLifecycleDecision(
            reason=reason,
            identity=binding.identity,
            active_holders=active_holders,
            binding_to_release=binding,
            lease_id=lease.id,
            runtime_to_stop=runtime_to_stop,
            grace_deadline=grace_deadline,
        )

    def _add_holder_locked(
        self,
        lease_id: UUID,
        binding: WorkspaceBinding[IdentityT, RuntimeT],
    ) -> None:
        state = self._workspaces.get(binding.identity)
        if state is None:
            state = _WorkspaceState(runtime=binding.runtime, holders=set())
            self._workspaces[binding.identity] = state
        elif state.runtime is not binding.runtime:
            raise ValueError("one workspace identity cannot have two live runtime instances")
        state.grace_deadline = None
        state.holders.add(lease_id)

    def _remove_holder_locked(
        self,
        lease_id: UUID,
        identity: IdentityT,
        now: float,
        *,
        immediate: bool = False,
    ) -> LeaseLifecycleDecision[IdentityT, RuntimeT]:
        state = self._workspaces[identity]
        state.holders.remove(lease_id)
        active_holders = len(state.holders)
        runtime_to_stop: RuntimeT | None = None
        grace_deadline: float | None = None
        if active_holders == 0:
            if immediate:
                runtime_to_stop = state.runtime
                del self._workspaces[identity]
            else:
                grace_deadline = now + self._warm_grace_seconds
                state.grace_deadline = grace_deadline
        return LeaseLifecycleDecision(
            reason=LeaseEndReason.RELEASED,
            identity=identity,
            active_holders=active_holders,
            runtime_to_stop=runtime_to_stop,
            grace_deadline=grace_deadline,
        )
