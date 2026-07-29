from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from serena_light.daemon.leases import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_EXPIRY_SECONDS,
    WARM_GRACE_SECONDS,
    DaemonLease,
    LeaseEndReason,
    LeaseExpiredError,
    LeaseLifecycle,
)
from serena_light.workspace.registry import WorkspaceBinding, WorkspaceLease


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def binding(identity: str, runtime: object | None = None) -> WorkspaceBinding[str, object]:
    instance = object() if runtime is None else runtime
    return WorkspaceBinding(
        lease=WorkspaceLease(id=uuid4(), identity=identity, runtime=instance),
        working_subdirectory=Path("."),
    )


def test_same_root_holders_are_distinct_and_share_lifetime_count() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    first = lifecycle.issue(binding("/data/project", runtime))
    second = lifecycle.issue(binding("/data/project", runtime))

    assert first.id != second.id
    assert lifecycle.active_holders("/data/project") == 2
    assert lifecycle.binding_for(first.id).runtime is lifecycle.binding_for(second.id).runtime is runtime


def test_lease_may_be_issued_unbound_then_attached_and_rebound_cross_root() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease = lifecycle.acquire_lease()

    assert lease.binding is None
    with pytest.raises(ValueError, match="no active workspace"):
        lifecycle.binding_for(lease.id)

    first = binding("/data/one")
    attached = lifecycle.rebind(lease.id, first)
    assert attached.binding == first
    assert lifecycle.active_holders("/data/one") == 1

    second = binding("/data/two")
    rebound = lifecycle.rebind(lease.id, second)
    assert rebound.binding == second
    assert lifecycle.active_holders("/data/one") == 0
    assert lifecycle.grace_deadline("/data/one") == WARM_GRACE_SECONDS
    assert lifecycle.active_holders("/data/two") == 1


def test_release_workspace_preserves_live_lease_and_starts_warm_grace() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease = lifecycle.issue(binding("/data/project"))

    released = lifecycle.release_workspace(lease.id)

    assert released.released
    assert released.decision is not None
    assert released.decision.binding_to_release == lease.binding
    assert released.decision.grace_deadline == WARM_GRACE_SECONDS
    assert lifecycle.require_active(lease.id).binding is None
    assert lifecycle.heartbeat(lease.id).id == lease.id
    with pytest.raises(ValueError, match="no active workspace"):
        lifecycle.binding_for(lease.id)


def test_immediate_workspace_release_stops_only_the_last_holder() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    first = lifecycle.issue(binding("/data/project", runtime))
    second = lifecycle.issue(binding("/data/project", runtime))

    first_release = lifecycle.release_workspace(first.id, immediate=True)
    assert first_release.released
    assert first_release.decision is not None
    assert first_release.decision.active_holders == 1
    assert first_release.decision.runtime_to_stop is None
    assert lifecycle.require_active(first.id).binding is None
    assert lifecycle.binding_for(second.id).runtime is runtime

    second_release = lifecycle.release_workspace(second.id, immediate=True)
    assert second_release.released
    assert second_release.decision is not None
    assert second_release.decision.active_holders == 0
    assert second_release.decision.runtime_to_stop is runtime
    assert second_release.decision.grace_deadline is None
    assert lifecycle.require_active(second.id).binding is None


def test_releasing_unbound_lease_has_no_workspace_effect() -> None:
    lifecycle = LeaseLifecycle[str, object](clock=FakeClock())
    lease = lifecycle.acquire_lease()
    assert lifecycle.active_lease_count() == 1

    result = lifecycle.release_lease(lease.id)

    assert result.released
    assert result.decision is not None
    assert result.decision.identity is None
    assert result.decision.binding_to_release is None
    assert lifecycle.active_lease_count() == 0
    assert lifecycle.daemon_idle()


def test_daemon_idle_waits_for_active_lease_and_workspace_grace() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock, warm_grace_seconds=2)
    assert lifecycle.daemon_idle()

    lease = lifecycle.acquire_lease(binding("/data/project"))
    assert not lifecycle.daemon_idle()
    lifecycle.release_lease(lease.id)
    assert not lifecycle.daemon_idle()

    clock.advance(2)
    lifecycle.sweep()
    assert lifecycle.daemon_idle()


def test_distinct_workspace_roots_keep_independent_holder_counts() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    one = lifecycle.issue(binding("/data/one"))
    two = lifecycle.issue(binding("/data/two"))

    lifecycle.release(one.id)

    assert lifecycle.active_holders("/data/one") == 0
    assert lifecycle.active_holders("/data/two") == 1
    assert lifecycle.binding_for(two.id).identity == "/data/two"


def test_heartbeats_have_independent_cadence_metadata_and_renewal() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease = lifecycle.issue(binding("/data/project"))

    assert lease.heartbeat_due_at == HEARTBEAT_INTERVAL_SECONDS
    clock.advance(HEARTBEAT_INTERVAL_SECONDS)
    renewed = lifecycle.heartbeat(lease.id)

    assert renewed.last_heartbeat_at == HEARTBEAT_INTERVAL_SECONDS
    assert renewed.expires_at == HEARTBEAT_INTERVAL_SECONDS + LEASE_EXPIRY_SECONDS


def test_heartbeat_keeps_lease_live_while_workspace_work_is_blocked() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease = lifecycle.issue(binding("/data/project"))

    for _ in range(5):  # Models independent connector heartbeats during a blocking LSP call.
        clock.advance(HEARTBEAT_INTERVAL_SECONDS)
        lifecycle.heartbeat(lease.id)

    assert lifecycle.require_active(lease.id).id == lease.id


def test_expiry_occurs_at_the_exact_boundary_and_later_use_is_typed() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease = lifecycle.issue(binding("/data/project"))

    clock.advance(LEASE_EXPIRY_SECONDS)
    with pytest.raises(LeaseExpiredError) as raised:
        lifecycle.binding_for(lease.id)

    assert raised.value.code == "LEASE_EXPIRED"
    assert raised.value.decision is not None
    assert raised.value.decision.reason is LeaseEndReason.EXPIRED
    assert raised.value.decision.binding_to_release == lease.binding
    assert lifecycle.active_holders("/data/project") == 0
    with pytest.raises(LeaseExpiredError):
        lifecycle.binding_for(lease.id)


def test_unknown_uuid_is_indistinguishable_from_an_expired_lease() -> None:
    lifecycle = LeaseLifecycle[str, object](clock=FakeClock())

    with pytest.raises(LeaseExpiredError) as raised:
        lifecycle.require_active(uuid4())

    assert raised.value.code == "LEASE_EXPIRED"
    assert raised.value.decision is None


def test_expiry_sweep_releases_only_expired_bindings() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    expired = lifecycle.issue(binding("/data/project", runtime))
    clock.advance(1)
    live = lifecycle.issue(binding("/data/project", runtime))

    clock.advance(LEASE_EXPIRY_SECONDS - 1)
    decisions = lifecycle.sweep()

    assert [decision.lease_id for decision in decisions] == [expired.id]
    assert decisions[0].binding_to_release == expired.binding
    assert lifecycle.active_holders("/data/project") == 1
    assert lifecycle.binding_for(live.id).identity == "/data/project"


def test_nonlast_immediate_release_never_stops_shared_runtime() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    first = lifecycle.issue(binding("/data/project", runtime))
    lifecycle.issue(binding("/data/project", runtime))

    result = lifecycle.release(first.id, immediate=True)

    assert result.released
    assert result.decision is not None
    assert result.decision.active_holders == 1
    assert result.decision.runtime_to_stop is None
    assert lifecycle.grace_deadline("/data/project") is None


def test_last_immediate_release_stops_without_grace_and_release_is_idempotent() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease = lifecycle.issue(binding("/data/project", runtime))

    result = lifecycle.release(lease.id, immediate=True)

    assert result.released
    assert result.decision is not None
    assert result.decision.runtime_to_stop is runtime
    assert result.decision.grace_deadline is None
    assert not lifecycle.release(lease.id, immediate=True).released
    assert not lifecycle.release_lease(lease.id).released
    assert lifecycle.active_holders("/data/project") == 0


def test_last_normal_release_starts_grace_reacquire_cancels_and_sweep_stops_after_deadline() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    first = lifecycle.issue(binding("/data/project", runtime))

    released = lifecycle.release(first.id)
    assert released.decision is not None
    assert released.decision.grace_deadline == WARM_GRACE_SECONDS
    clock.advance(WARM_GRACE_SECONDS - 1)
    assert lifecycle.sweep() == ()

    second = lifecycle.issue(binding("/data/project", runtime))
    assert lifecycle.grace_deadline("/data/project") is None
    lifecycle.release(second.id)
    clock.advance(WARM_GRACE_SECONDS)

    decisions = lifecycle.sweep()
    assert len(decisions) == 1
    assert decisions[0].reason is LeaseEndReason.GRACE_EXPIRED
    assert decisions[0].runtime_to_stop is runtime


def test_concurrent_issue_produces_unique_daemon_uuids_and_consistent_holders() -> None:
    clock = FakeClock()
    runtime = object()
    lifecycle = LeaseLifecycle[str, object](clock=clock)

    with ThreadPoolExecutor(max_workers=16) as executor:
        leases = list(executor.map(lambda _: lifecycle.issue(binding("/data/project", runtime)), range(128)))

    assert len({lease.id for lease in leases}) == len(leases)
    assert lifecycle.active_holders("/data/project") == len(leases)


def test_lease_snapshots_are_immutable() -> None:
    clock = FakeClock()
    lifecycle = LeaseLifecycle[str, object](clock=clock)
    lease: DaemonLease[str, object] = lifecycle.issue(binding("/data/project"))

    with pytest.raises(AttributeError):
        lease.last_heartbeat_at = 1  # type: ignore[misc]
