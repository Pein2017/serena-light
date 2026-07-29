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
from typing import Any, Protocol, cast
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
from serena_light.lsp.adapter import AdapterError, LspProcessLost
from serena_light.lsp.client import LspProtocolError, LspResponseError, LspTransportClosed
from serena_light.lsp.executor import ExecutorBusyError
from serena_light.tools.envelopes import (
    ErrorCode,
    JsonValue,
    error,
    from_adapter_error,
    from_executor_busy,
    from_timeout,
    from_workspace_error,
    success,
)
from serena_light.workspace.identity import WorkspaceError, WorkspaceIdentity, WorkspacePolicy
from serena_light.workspace.registry import (
    PreparedWorkspaceActivation,
    ResolvedWorkspace,
    WorkspaceBinding,
    WorkspaceRuntimeRegistry,
)
from serena_light.workspace.runtime import WorkspaceRuntimeError

# The only semantic operation that can install a write.  An ordinary LSP
# failure elsewhere is a read that provably never wrote anything, so it must
# not be declared with a code that could imply otherwise.
_LSP_WRITE_OPERATIONS = frozenset({"replace_symbol_body"})


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
        debug_reporter: Callable[[str, str], object] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._registry = registry
        self._resolver = resolver
        self._runtime_stopper = runtime_stopper
        self._debug_reporter = debug_reporter
        self._binding_lock = asyncio.Lock()
        self._runtime_stop_lock = asyncio.Lock()
        self._pending_runtime_stops: dict[int, RuntimeT] = {}

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        """Return transport correlation without granting lifetime authority."""

        return {"mcp_session_id": mcp_session_id, "lifetime_authority": "daemon_lease"}

    async def migration_status(self) -> Mapping[str, object]:
        """Expose only authenticated lifetime facts needed for legacy retirement."""

        return {
            "active_holders": self._lifecycle.active_lease_count(),
            "daemon_idle": self.daemon_idle(),
        }

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
        stopped = await self._stop_runtimes(runtimes_to_stop)
        self._report_decision(result.decision, immediate=immediate)
        return _release_data(daemon_lease_id, result, immediate=immediate, stopped=stopped)

    async def release_workspace(self, *, lease_id: str, immediate: bool = False) -> Mapping[str, object]:
        """Drop the current binding while retaining the same live lease UUID."""

        if type(immediate) is not bool:
            return error(ErrorCode.INVALID_INPUT, details={"field": "immediate"}).to_dict()
        daemon_lease_id = _lease_uuid(lease_id)
        async with self._binding_lock:
            try:
                result = self._lifecycle.release_workspace(daemon_lease_id, immediate=immediate)
            except LifecycleLeaseExpiredError as exc:
                runtimes_to_stop = self._apply_decision_locked(exc.decision)
                expiry_error: LifecycleLeaseExpiredError[IdentityT, RuntimeT] | None = exc
            else:
                runtimes_to_stop = self._apply_result_locked(result)
                expiry_error = None
        stopped = await self._stop_runtimes(runtimes_to_stop)
        self._report_decision(
            expiry_error.decision if expiry_error is not None else result.decision,
            immediate=immediate,
        )
        if expiry_error is not None:
            raise LeaseExpiredError(str(expiry_error)) from expiry_error
        return _release_workspace_data(daemon_lease_id, result, immediate=immediate, stopped=stopped)

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        """Resolve, refresh, then atomically bind one live daemon lease."""

        daemon_lease_id = _lease_uuid(lease_id)
        activation_path = Path(absolute_path)
        if not activation_path.is_absolute():
            return error(ErrorCode.INVALID_PATH, details={"path": absolute_path}).to_dict()
        try:
            resolved = await asyncio.to_thread(self._resolve_workspace, activation_path)
        except WorkspaceError as exc:
            # Resolution precedes registry/lifecycle mutation, so failures keep
            # the prior binding and lease authority unchanged.
            return from_workspace_error(exc).to_dict()
        except Exception as exc:
            return self._activation_error(exc)

        runtimes_to_stop: list[RuntimeT] = []
        expiry_error: LifecycleLeaseExpiredError[IdentityT, RuntimeT] | None = None
        binding: WorkspaceBinding[IdentityT, RuntimeT] | None = None
        prepared: PreparedWorkspaceActivation[IdentityT, RuntimeT, UUID] | None = None
        committed = False
        activation_error: Mapping[str, object] | None = None
        async with self._binding_lock:
            try:
                # Reject a release/expiry that happened while path resolution ran
                # before acquiring a registry lease or constructing a runtime.
                self._lifecycle.require_active(daemon_lease_id)
            except LifecycleLeaseExpiredError as exc:
                expiry_error = exc
                runtimes_to_stop.extend(self._apply_decision_locked(exc.decision))
            else:
                try:
                    prepared = await asyncio.to_thread(
                        self._registry.prepare_activation,
                        daemon_lease_id,
                        resolved,
                    )
                    await self._ensure_runtime_fresh(prepared.binding.runtime)
                    # Refresh runs off-loop and can outlive lease authority.
                    # Check again before publishing any provisional binding.
                    self._lifecycle.require_active(daemon_lease_id)
                    binding = await asyncio.to_thread(self._registry.commit_activation, prepared)
                    committed = True
                    self._lifecycle.rebind(daemon_lease_id, binding)
                except LifecycleLeaseExpiredError as exc:
                    expiry_error = exc
                    if prepared is not None:
                        runtimes_to_stop.extend(
                            self._discard_prepared_activation_locked(
                                daemon_lease_id,
                                prepared,
                                committed=committed,
                            )
                        )
                    runtimes_to_stop.extend(self._apply_decision_locked(exc.decision))
                except Exception as exc:
                    if prepared is not None:
                        runtimes_to_stop.extend(
                            self._discard_prepared_activation_locked(
                                daemon_lease_id,
                                prepared,
                                committed=committed,
                            )
                        )
                    activation_error = self._activation_error(exc)

        await self._stop_runtimes(runtimes_to_stop)
        if expiry_error is not None:
            raise LeaseExpiredError(str(expiry_error)) from expiry_error
        if activation_error is not None:
            return activation_error
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
        for decision in decisions:
            self._report_decision(decision, immediate=False)
        return decisions

    def daemon_idle(self) -> bool:
        """Expose only the build-retirement predicate, not mutable lease state."""

        return self._lifecycle.daemon_idle() and not self._pending_runtime_stops

    def _report_decision(
        self,
        decision: LeaseLifecycleDecision[IdentityT, RuntimeT] | None,
        *,
        immediate: bool,
    ) -> None:
        if decision is None or self._debug_reporter is None:
            return
        event = "workspace_cleanup" if decision.runtime_to_stop is not None else "lease_grace"
        message = (
            f"reason={decision.reason.value} holders={decision.active_holders} "
            f"immediate={str(immediate).lower()}"
        )
        self._debug_reporter(event, message)

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

    async def _ensure_runtime_fresh(self, runtime: RuntimeT) -> None:
        """Refresh a provisional runtime before committing its lease binding."""

        try:
            callback = cast(Callable[[], object], cast(Any, runtime).ensure_fresh)
        except AttributeError:
            return
        result = await asyncio.to_thread(callback)
        if isawaitable(result):
            await result

    def _discard_prepared_activation_locked(
        self,
        lease_id: UUID,
        prepared: PreparedWorkspaceActivation[IdentityT, RuntimeT, UUID],
        *,
        committed: bool,
    ) -> list[RuntimeT]:
        """Discard an uncommitted candidate or a post-expiry committed binding."""

        if committed:
            self._registry.release(lease_id)
        else:
            self._registry.abort_activation(prepared)
        if not prepared.candidate_runtime_created:
            return []
        detached = self._registry.retire_idle(prepared.binding.identity, prepared.binding.runtime)
        return [] if detached is None else [detached]

    @staticmethod
    def _activation_error(exc: Exception) -> Mapping[str, object]:
        """Convert activation setup failures without leaking exception detail."""

        if isinstance(exc, WorkspaceError):
            return from_workspace_error(exc).to_dict()
        if isinstance(exc, WorkspaceRuntimeError):
            try:
                code = ErrorCode(exc.code)
            except ValueError:
                code = ErrorCode.UNSUPPORTED
            details: dict[str, JsonValue] = {"paths": exc.paths} if exc.paths else {}
            return error(code, details=details).to_dict()
        if isinstance(exc, ExecutorBusyError):
            return from_executor_busy(exc).to_dict()
        if isinstance(exc, AdapterError):
            return from_adapter_error(exc).to_dict()
        if isinstance(exc, TimeoutError):
            return from_timeout(exc).to_dict()
        return error(ErrorCode.UNCERTAIN).to_dict()

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
        if result.get("ok") is True and "data" in result:
            return result
        if result.get("ok") is False and isinstance(result.get("error"), Mapping):
            return result
        return error(ErrorCode.UNSUPPORTED, details={"tool": operation, "reason": "malformed_runtime_result"}).to_dict()

    @staticmethod
    def _lsp_failure_envelope(operation: str) -> Mapping[str, object]:
        """Translate an ordinary LSP response/protocol failure by write risk.

        A write operation must fail conservatively: it may already have
        installed a replacement, so ``UNCERTAIN`` is the only honest code.  A
        read never installs anything, so it is declared ``UNSUPPORTED`` with a
        bounded reason -- never a code that could imply a possible write.
        """

        if operation in _LSP_WRITE_OPERATIONS:
            return error(ErrorCode.UNCERTAIN, retry=None).to_dict()
        return error(ErrorCode.UNSUPPORTED, details={"tool": operation, "reason": "lsp_failure"}).to_dict()

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
        except WorkspaceRuntimeError as exc:
            try:
                code = ErrorCode(exc.code)
            except ValueError:
                code = ErrorCode.UNSUPPORTED
            details: dict[str, JsonValue] = {"paths": exc.paths} if exc.paths else {}
            return error(code, details=details).to_dict()
        except ExecutorBusyError as exc:
            return from_executor_busy(exc).to_dict()
        except AdapterError as exc:
            return from_adapter_error(exc).to_dict()
        # TimeoutError is an OSError, so preserve its stronger operational
        # meaning before the generic OSError fallback below.
        except TimeoutError as exc:
            return from_timeout(exc).to_dict()
        # An ordinary LSP response/protocol failure (semantic lookup or
        # pre-install edit resolution), or a transport/process loss the
        # adapter's own retry already exhausted, is a runtime/service
        # boundary concern, not a programming error; translate it without
        # exposing its message.
        except (LspResponseError, LspProtocolError, LspTransportClosed, LspProcessLost):
            return self._lsp_failure_envelope(operation)
        except OSError:
            return error(ErrorCode.UNCERTAIN, retry=None).to_dict()
        if isawaitable(result):
            try:
                result = await result
            except WorkspaceError as exc:
                return from_workspace_error(exc).to_dict()
            except WorkspaceRuntimeError as exc:
                try:
                    code = ErrorCode(exc.code)
                except ValueError:
                    code = ErrorCode.UNSUPPORTED
                details = {"paths": exc.paths} if exc.paths else {}
                return error(code, details=details).to_dict()
            except ExecutorBusyError as exc:
                return from_executor_busy(exc).to_dict()
            except AdapterError as exc:
                return from_adapter_error(exc).to_dict()
            except TimeoutError as exc:
                return from_timeout(exc).to_dict()
            except (LspResponseError, LspProtocolError, LspTransportClosed, LspProcessLost):
                return self._lsp_failure_envelope(operation)
            except OSError:
                return error(ErrorCode.UNCERTAIN).to_dict()
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

    async def _stop_runtimes(self, runtimes: list[RuntimeT]) -> bool:
        """Best-effort stop of every pending runtime, old or newly retired.

        A stop failure never raises here: it would otherwise contaminate
        whichever unrelated lease/root operation happened to trigger this
        retry, or terminate the periodic sweep loop, after that operation's
        own binding/lifecycle change had already committed.  A failed runtime
        simply remains pending for the next retry, and non-idleness is the
        only public signal (see ``daemon_idle``/``migration_status``).

        Returns whether every runtime passed in *this* call (the caller's own
        detached targets) is confirmed stopped by the time this call returns.
        Old pending entries retried alongside are always best-effort and
        never affect this return value -- only the caller's own targets do.
        """

        own_targets = {id(runtime) for runtime in runtimes}
        async with self._runtime_stop_lock:
            for runtime in runtimes:
                self._pending_runtime_stops.setdefault(id(runtime), runtime)
            pending = tuple(self._pending_runtime_stops.items())
            if not pending:
                return True
            results = await asyncio.gather(
                *(asyncio.to_thread(self._runtime_stopper, runtime) for _, runtime in pending),
                return_exceptions=True,
            )
            own_targets_stopped = True
            for (marker, runtime), result in zip(pending, results, strict=True):
                if isinstance(result, BaseException):
                    self._pending_runtime_stops[marker] = runtime
                    if marker in own_targets:
                        own_targets_stopped = False
                else:
                    self._pending_runtime_stops.pop(marker, None)
            return own_targets_stopped


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


def _stop_fields[IdentityT, RuntimeT](
    decision: LeaseLifecycleDecision[IdentityT, RuntimeT] | None,
    *,
    stopped: bool,
) -> dict[str, object]:
    """Report public stop truth: decided, and actually confirmed, separately.

    ``stopped`` reflects only the caller's own detached target(s) (see
    ``_stop_runtimes``); a decision with no target to stop is neither stopped
    nor pending.
    """

    decided = decision is not None and decision.runtime_to_stop is not None
    return {
        "runtime_stopped": decided and stopped,
        "runtime_stop_pending": decided and not stopped,
    }


def _release_data[IdentityT, RuntimeT](
    lease_id: UUID,
    result: ReleaseResult[IdentityT, RuntimeT],
    *,
    immediate: bool,
    stopped: bool,
) -> dict[str, object]:
    decision = result.decision
    return {
        "lease_id": str(lease_id),
        "released": result.released,
        "immediate": immediate,
        "reason": None if decision is None else decision.reason.value,
        "active_holders": None if decision is None else decision.active_holders,
        "grace_deadline": None if decision is None else decision.grace_deadline,
        **_stop_fields(decision, stopped=stopped),
    }


def _release_workspace_data[IdentityT, RuntimeT](
    lease_id: UUID,
    result: ReleaseResult[IdentityT, RuntimeT],
    *,
    immediate: bool,
    stopped: bool,
) -> dict[str, object]:
    decision = result.decision
    return {
        "lease_id": str(lease_id),
        "released": result.released,
        "bound": False,
        "immediate": immediate,
        "reason": None if decision is None else decision.reason.value,
        "active_holders": None if decision is None else decision.active_holders,
        "grace_deadline": None if decision is None else decision.grace_deadline,
        **_stop_fields(decision, stopped=stopped),
    }
