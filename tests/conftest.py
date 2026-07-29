"""Admission gates for tests that inspect mutable external roots."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import pytest

from scripts.external_snapshot import snapshot_identity


@dataclass(frozen=True, slots=True)
class _SnapshotGate:
    root: Path
    environment_name: str
    observed: str


SNAPSHOT_GATES_KEY: pytest.StashKey[tuple[_SnapshotGate, ...]] = pytest.StashKey()
_skip = cast(Callable[[str], NoReturn], pytest.skip)
_fail = cast(Callable[[str], NoReturn], pytest.fail)


def pytest_runtest_setup(item: pytest.Item) -> None:
    gates = tuple(_snapshot_gates(item))
    missing = tuple(gate for gate in gates if gate.environment_name not in os.environ)
    if missing:
        observed = ", ".join(f"{gate.environment_name}={gate.observed!r}" for gate in missing)
        assignments = " ".join(f"{gate.environment_name}={shlex.quote(gate.observed)}" for gate in missing)
        _skip(f"external root snapshot required: observed {observed}; run {assignments} uv run pytest -q {item.nodeid}")
    for gate in gates:
        expected = os.environ[gate.environment_name]
        if expected != gate.observed:
            _fail(
                f"external root snapshot mismatch for {gate.root}: expected {gate.environment_name}={expected!r}, "
                f"observed {gate.observed!r}; refresh with uv run python scripts/print_external_snapshots.py"
            )
    item.stash[SNAPSHOT_GATES_KEY] = gates
    if item.get_closest_marker("performance_external") is not None and os.environ.get(
        "SERENA_LIGHT_RUN_PERFORMANCE_ACCEPTANCE"
    ) != "1":
        _skip("performance external acceptance requires SERENA_LIGHT_RUN_PERFORMANCE_ACCEPTANCE=1")


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    del nextitem
    for gate in item.stash.get(SNAPSHOT_GATES_KEY, ()):
        observed_after = snapshot_identity(gate.root)
        if observed_after != gate.observed:
            _fail(
                f"external root changed during {item.nodeid}: {gate.root}; "
                f"before={gate.observed!r}, after={observed_after!r}"
            )
        expected = os.environ[gate.environment_name]
        if expected != observed_after:
            _fail(
                f"external root snapshot no longer matches {gate.environment_name} after {item.nodeid}: "
                f"expected {expected!r}, observed {observed_after!r}"
            )


def _snapshot_gates(item: pytest.Item) -> list[_SnapshotGate]:
    gates: list[_SnapshotGate] = []
    for mark in item.iter_markers(name="external_repo"):
        root = mark.kwargs.get("root")
        environment_name = mark.kwargs.get("snapshot_env")
        if not isinstance(root, str) or not isinstance(environment_name, str):
            raise pytest.UsageError(
                "external_repo markers require string root=... and snapshot_env=... keyword arguments"
            )
        gates.append(_SnapshotGate(Path(root), environment_name, snapshot_identity(Path(root))))
    return gates
