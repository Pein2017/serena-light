"""Async orchestration between daemon leases and shared workspace runtimes.

The MCP session identifier is deliberately absent from the binding registry.
It is transport correlation only; a daemon-issued UUID is both the registry
session key and the sole authority accepted by workspace operations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable, Mapping
from inspect import isawaitable
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from serena_light.daemon.leases import (
    DaemonLease,
    LeaseLifecycle,
    LeaseLifecycleDecision,
    ReleaseResult,
)
from serena_light.daemon.leases import (
    LeaseExpiredError as LifecycleLeaseExpiredError,
)
from serena_light.daemon.server import LeaseExpiredError
from serena_light.tools.envelopes import ErrorCode, JsonValue, error, from_workspace_error, success
from serena_light.workspace.identity import WorkspaceError, WorkspaceIdentity, WorkspacePolicy
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceBinding, WorkspaceRuntimeRegistry


class WorkspaceResolver[IdentityT](Protocol):
    """Resolve an activation path without mutating daemon-owned state."""

    def __call__(self, activation_path: Path, /) -> ResolvedWorkspace[IdentityT] | WorkspaceIdentity: ...


class WorkspaceDaemonService[IdentityT: Hashable, RuntimeT]:
    """Concrete lease/registry implementation of :class:`DaemonService`.

    Blocking identity resolution, runtime construction, and runtime stopping
    run in worker threads.  ``_binding_lock`` protects only the compound
    registry/lifecycle transitions.  Heartbeats intentionally bypass it and
    never depend on workspace acquisition or LSP progress.
    """

    def __init__(
        self,
        *,
        lifecycle: LeaseLifecycle[IdentityT, RuntimeT],
        registry: WorkspaceRuntimeRegistry[IdentityT, RuntimeT, UUID],
        resolver: WorkspaceResolver[IdentityT] | WorkspacePolicy,
        runtime_stopper: Callable[[RuntimeT], None],
    ) -> None:
        self._lifecycle = lifecycle
        self._registry = registry
        self._resolver = resolver
        self._runtime_stopper = runtime_stopper
        self._binding_lock = asyncio.Lock()

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        """Return transport correlation without granting lifetime authority."""

        return {"mcp_session_id": mcp_session_id, "lifetime_authority": "daemon_lease"}

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        """Issue an unbound daemon lease independently of the MCP session."""

        del mcp_session_id
        lease = self._lifecycle.acquire_lease()
        return _lease_data(lease)

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        """Renew a lease without waiting for workspace binding work."""

        daemon_lease_id = _lease_uuid(lease_id)
        try:
            lease = self._lifecycle.heartbeat(daemon_lease_id)
        except LifecycleLeaseExpiredError as error:
            await self._clean_expired(error)
            raise LeaseExpiredError(str(error)) from error
        return _lease_data(lease)

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        """Release one daemon authority and apply its registry/stop effects."""

        daemon_lease_id = _lease_uuid(lease_id)
        runtimes_to_stop: list[RuntimeT]
        async with self._binding_lock:
            result = self._lifecycle.release_lease(daemon_lease_id, immediate=immediate)
            runtimes_to_stop = self._apply_result_locked(result)
        await self._stop_runtimes(runtimes_to_stop)
        return _release_data(daemon_lease_id, result, immediate=immediate)

    async def release_workspace(self, *, lease_id: str) -> Mapping[str, object]:
        """Drop the current binding while retaining the same live lease UUID."""

        daemon_lease_id = _lease_uuid(lease_id)
        async with self._binding_lock:
            try:
                result = self._lifecycle.release_workspace(daemon_lease_id)
            except LifecycleLeaseExpiredError as error:
                runtimes_to_stop = self._apply_decision_locked(error.decision)
                expiry_error: LifecycleLeaseExpiredError[IdentityT, RuntimeT] | None = error
            else:
                runtimes_to_stop = self._apply_result_locked(result)
                expiry_error = None
        await self._stop_runtimes(runtimes_to_stop)
        if expiry_error is not None:
            raise LeaseExpiredError(str(expiry_error)) from expiry_error
        return _release_workspace_data(daemon_lease_id, result)

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        """Resolve off-loop, then acquire-swap and bind one live daemon lease."""

        daemon_lease_id = _lease_uuid(lease_id)
        activation_path = Path(absolute_path)
        if not activation_path.is_absolute():
            raise ValueError("absolute_path must be absolute")
        resolved = await asyncio.to_thread(self._resolve_workspace, activation_path)

        runtimes_to_stop: list[RuntimeT] = []
        expiry_error: LifecycleLeaseExpiredError[IdentityT, RuntimeT] | None = None
        binding: WorkspaceBinding[IdentityT, RuntimeT] | None = None
        async with self._binding_lock:
            try:
                # Reject a release/expiry that happened while path resolution ran
                # before acquiring a registry lease or constructing a runtime.
                self._lifecycle.require_active(daemon_lease_id)
            except LifecycleLeaseExpiredError as error:
                expiry_error = error
                runtimes_to_stop.extend(self._apply_decision_locked(error.decision))
            else:
                binding = await asyncio.to_thread(self._registry.activate, daemon_lease_id, resolved)
                try:
                    self._lifecycle.rebind(daemon_lease_id, binding)
                except LifecycleLeaseExpiredError as error:
                    expiry_error = error
                    # Runtime acquisition completed after authority expired.  It
                    # never entered lifecycle grace ownership, so retire it now.
                    self._registry.release(daemon_lease_id)
                    detached = self._registry.retire_idle(binding.identity, binding.runtime)
                    if detached is not None:
                        runtimes_to_stop.append(detached)
                    runtimes_to_stop.extend(self._apply_decision_locked(error.decision))

        await self._stop_runtimes(runtimes_to_stop)
        if expiry_error is not None:
            raise LeaseExpiredError(str(expiry_error)) from expiry_error
        assert binding is not None
        return {
            "lease_id": str(daemon_lease_id),
            "workspace": _binding_data(binding),
        }

    async def binding_for(self, *, lease_id: str) -> WorkspaceBinding[IdentityT, RuntimeT]:
        """Resolve workspace state only through a currently live daemon UUID."""

        daemon_lease_id = _lease_uuid(lease_id)
        try:
            return self._lifecycle.binding_for(daemon_lease_id)
        except LifecycleLeaseExpiredError as error:
            await self._clean_expired(error)
            raise LeaseExpiredError(str(error)) from error

    async def get_runtime_status(self, *, lease_id: str) -> Mapping[str, object]:
        """Return binding-local, secret-free runtime status for one live lease."""

        daemon_lease_id = _lease_uuid(lease_id)
        try:
            lease = self._lifecycle.require_active(daemon_lease_id)
        except LifecycleLeaseExpiredError as error:
            await self._clean_expired(error)
            raise LeaseExpiredError(str(error)) from error
        if lease.binding is None:
            return success(cast(JsonValue, {"lease": _lease_data(lease), "binding": None, "runtime": None})).to_dict()
        runtime_status = await self._runtime_value(lease.binding.runtime, "status", {})
        return success(
            cast(
                JsonValue,
                {
                "lease": _lease_data(lease),
                "binding": _binding_data(lease.binding),
                "runtime": runtime_status,
                },
            )
        ).to_dict()

    async def semantic_operation(
        self, *, lease_id: str, operation: str, **kwargs: object
    ) -> Mapping[str, object]:
        """Delegate one registered semantic operation through the lease binding."""

        return await self._semantic_call(lease_id, operation, **kwargs)

    async def sweep(self) -> tuple[LeaseLifecycleDecision[IdentityT, RuntimeT], ...]:
        """Apply 60-second expiry and ten-minute warm-grace decisions."""

        async with self._binding_lock:
            decisions = self._lifecycle.sweep()
            runtimes_to_stop = self._apply_decisions_locked(decisions)
        await self._stop_runtimes(runtimes_to_stop)
        return decisions

    def _resolve_workspace(self, activation_path: Path) -> ResolvedWorkspace[IdentityT]:
        if isinstance(self._resolver, WorkspacePolicy):
            result: ResolvedWorkspace[IdentityT] | WorkspaceIdentity = self._resolver.resolve_activation(
                activation_path
            )
        else:
            result = self._resolver(activation_path)
        if isinstance(result, WorkspaceIdentity):
            # Working-subdirectory metadata is connection-local, so only the
            # physical root key participates in runtime reuse.
            return ResolvedWorkspace(
                identity=cast(IdentityT, result.registry_key),
                working_subdirectory=result.working_subdirectory,
            )
        if not isinstance(result, ResolvedWorkspace):
            raise TypeError("workspace resolver must return ResolvedWorkspace or WorkspaceIdentity")
        return result

    async def _clean_expired(self, error: LifecycleLeaseExpiredError[IdentityT, RuntimeT]) -> None:
        async with self._binding_lock:
            runtimes_to_stop = self._apply_decision_locked(error.decision)
        await self._stop_runtimes(runtimes_to_stop)

    async def _semantic_call(
        self, lease_id: str, operation: str, **kwargs: object
    ) -> Mapping[str, object]:
        try:
            binding = await self.binding_for(lease_id=lease_id)
        except ValueError:
            return error(ErrorCode.INVALID_INPUT, details={"field": "active_workspace"}).to_dict()
        return await self._runtime_result(binding.runtime, operation, kwargs)

    async def _runtime_result(
        self, runtime: RuntimeT, operation: str, kwargs: Mapping[str, object]
    ) -> Mapping[str, object]:
        result = await self._runtime_value(runtime, operation, kwargs)
        return result if isinstance(result.get("ok"), bool) else success(cast(JsonValue, result)).to_dict()

    async def _runtime_value(
        self, runtime: RuntimeT, operation: str, kwargs: Mapping[str, object]
    ) -> Mapping[str, object]:
        callback = getattr(runtime, operation, None)
        if not callable(callback):
            return error(ErrorCode.UNSUPPORTED, details={"tool": operation}).to_dict()
        try:
            result = await asyncio.to_thread(callback, **kwargs)
        except WorkspaceError as exc:
            return from_workspace_error(exc).to_dict()
        if isawaitable(result):
            result = await result
        if callable(to_dict := getattr(result, "to_dict", None)):
            result = to_dict()
        if isinstance(result, Mapping):
            return dict(cast(Mapping[str, object], result))
        raise TypeError(f"runtime {operation} must return ToolEnvelope or Mapping")

    def _apply_result_locked(self, result: ReleaseResult[IdentityT, RuntimeT]) -> list[RuntimeT]:
        return self._apply_decision_locked(result.decision)

    def _apply_decisions_locked(
        self,
        decisions: tuple[LeaseLifecycleDecision[IdentityT, RuntimeT], ...],
    ) -> list[RuntimeT]:
        runtimes: list[RuntimeT] = []
        for decision in decisions:
            runtimes.extend(self._apply_decision_locked(decision))
        return runtimes

    def _apply_decision_locked(
        self,
        decision: LeaseLifecycleDecision[IdentityT, RuntimeT] | None,
    ) -> list[RuntimeT]:
        if decision is None:
            return []
        if decision.binding_to_release is not None:
            self._registry.release_lease(decision.binding_to_release.lease.id)
        if decision.identity is None or decision.runtime_to_stop is None:
            return []
        detached = self._registry.retire_idle(decision.identity, decision.runtime_to_stop)
        return [] if detached is None else [detached]

    async def _stop_runtimes(self, runtimes: list[RuntimeT]) -> None:
        unique: list[RuntimeT] = []
        seen: set[int] = set()
        for runtime in runtimes:
            marker = id(runtime)
            if marker not in seen:
                seen.add(marker)
                unique.append(runtime)
        if unique:
            await asyncio.gather(*(asyncio.to_thread(self._runtime_stopper, runtime) for runtime in unique))


def _lease_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise LeaseExpiredError("workspace lease has expired") from error


def _lease_data[IdentityT, RuntimeT](lease: DaemonLease[IdentityT, RuntimeT]) -> dict[str, object]:
    data: dict[str, object] = {
        "lease_id": str(lease.id),
        "bound": lease.binding is not None,
        "issued_at": lease.issued_at,
        "last_heartbeat_at": lease.last_heartbeat_at,
        "heartbeat_interval_seconds": lease.heartbeat_interval_seconds,
        "expiry_seconds": lease.expiry_seconds,
    }
    if lease.binding is not None:
        data["workspace"] = _binding_data(lease.binding)
    return data


def _binding_data[IdentityT, RuntimeT](binding: WorkspaceBinding[IdentityT, RuntimeT]) -> dict[str, object]:
    identity: object = binding.identity
    if isinstance(identity, tuple) and len(identity) == 2 and isinstance(identity[1], Path):
        identity = str(identity[1])
    elif isinstance(identity, Path) or (not isinstance(identity, bool | float | int | str) and identity is not None):
        identity = str(identity)
    return {
        "identity": identity,
        "working_subdirectory": str(binding.working_subdirectory),
    }


def _release_data[IdentityT, RuntimeT](
    lease_id: UUID,
    result: ReleaseResult[IdentityT, RuntimeT],
    *,
    immediate: bool,
) -> dict[str, object]:
    decision = result.decision
    return {
        "lease_id": str(lease_id),
        "released": result.released,
        "immediate": immediate,
        "reason": None if decision is None else decision.reason.value,
        "active_holders": None if decision is None else decision.active_holders,
        "grace_deadline": None if decision is None else decision.grace_deadline,
        "runtime_stopped": decision is not None and decision.runtime_to_stop is not None,
    }


def _release_workspace_data[IdentityT, RuntimeT](
    lease_id: UUID,
    result: ReleaseResult[IdentityT, RuntimeT],
) -> dict[str, object]:
    decision = result.decision
    return {
        "lease_id": str(lease_id),
        "released": result.released,
        "bound": False,
        "reason": None if decision is None else decision.reason.value,
        "active_holders": None if decision is None else decision.active_holders,
        "grace_deadline": None if decision is None else decision.grace_deadline,
    }
