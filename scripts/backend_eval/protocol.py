"""The Phase 2 protocol-plane interface and shared, deadline-bound probe runner.

Decision P2-1 (design.md): the protocol plane starts each locked backend directly through
process-launch and transport primitives -- ``LanguageServerSubprocessLauncher``,
``SubprocessAdapterRuntimeProvider``, ``SyncLspClient`` -- and never constructs a
``LanguageAdapter`` or ``WorkspaceRuntime``. Those own document lifecycle, freshness, lease,
and scope-generation state that belongs to Phase 3's product-seam plane. This module imports
only ``serena_light.lsp.{client,adapter}``, ``serena_light.processes``, and Phase 1's
``scripts.backend_eval.{process,runtime}`` -- never ``serena_light.workspace`` -- and
``tests/backend_eval/test_protocol.py`` asserts this by parsing this file's own imports.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.process import Deadline
from scripts.backend_eval.runtime import CandidateRuntime, minimal_backend_environment
from serena_light.lsp.adapter import EngineMetadata, RawLspProviders, SubprocessAdapterRuntimeProvider
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.positions import PositionEncoding
from serena_light.processes import LanguageServerSubprocessLauncher

__all__ = ["BackendProtocolSpec", "ProtocolSession", "run_protocol_probe"]

_DIAGNOSTICS_MODES = frozenset({"push", "pull"})


@dataclass(frozen=True, slots=True)
class BackendProtocolSpec:
    """The fixed, per-candidate facts ``run_protocol_probe`` needs to start one backend.

    Every field is a callable or literal frozen at construction time; no later task may
    introduce a second protocol-session representation (Task 1 freezes this shape for
    Tasks 2-8).
    """

    name: str
    build_command: Callable[[CandidateRuntime], tuple[str, ...]]
    initialize_params: Callable[[Path], Mapping[str, object]]
    request_handlers: Mapping[str, Callable[[Any], Any]] | None
    engine: Callable[[CandidateRuntime], EngineMetadata]
    position_encoding: PositionEncoding
    diagnostics_mode: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("BackendProtocolSpec.name must be non-empty")
        if self.diagnostics_mode not in _DIAGNOSTICS_MODES:
            raise ValueError(f"BackendProtocolSpec.diagnostics_mode must be one of {sorted(_DIAGNOSTICS_MODES)}")


@dataclass(frozen=True, slots=True)
class ProtocolSession[T]:
    """The bounded result of one ``run_protocol_probe`` call."""

    raw_providers: RawLspProviders
    engine: EngineMetadata
    result: T


def _proxy_free_environment(env: Mapping[str, str]) -> dict[str, str | None]:
    """Extend a minimal environment with explicit removal of every ambient ``*_PROXY`` key.

    ``LanguageServerSubprocessLauncher.launch`` merges its ``env`` argument onto a copy of
    the current process environment (production needs ambient inheritance for its own
    launches); only keys explicitly present are overridden or removed. ``minimal_backend_environment``
    does not itself carry proxy keys, so without this step an ambient proxy variable would
    still reach the candidate process through that merge, violating the no-inherited-proxy
    contract (design.md Decision 2, Global Constraints).
    """

    merged: dict[str, str | None] = dict(env)
    for key in os.environ:
        if key.upper().endswith("_PROXY"):
            merged[key] = None
    return merged


def run_protocol_probe[T](
    spec: BackendProtocolSpec,
    runtime: CandidateRuntime,
    workspace_root: Path,
    *,
    deadline: Deadline,
    session: Callable[[SyncLspClient], T],
) -> ProtocolSession[T]:
    """Launch ``spec``'s backend, run ``initialize``/``initialized``, then ``session``.

    ``client.shutdown()`` and ``SubprocessAdapterRuntimeProvider.stop()`` are always both
    called, even when ``session`` raises. A ``deadline.expired()`` observation -- whether
    raised before launch, before ``initialize``, before ``session``, or discovered only after
    ``session`` returns -- is never swallowed by cleanup: cleanup runs in a ``finally`` block
    and never catches or replaces the propagating exception.
    """

    deadline.check(f"{spec.name} protocol probe start")
    command = spec.build_command(runtime)
    env = _proxy_free_environment(minimal_backend_environment(runtime, runtime.python))
    provider = SubprocessAdapterRuntimeProvider(
        command=command,
        cwd=workspace_root,
        launcher=LanguageServerSubprocessLauncher.get_instance(),
        env=env,
        request_timeout=deadline.remaining(),
        request_handlers=spec.request_handlers,
    )
    terminal_errors: list[BaseException] = []
    adapter_runtime = provider.start(
        notification_handler=lambda _method, _params: None,
        terminal_handler=terminal_errors.append,
    )
    # SubprocessAdapterRuntimeProvider.start always constructs a real SyncLspClient; its
    # return type widens to the AdapterClient protocol for adapter-neutral callers.
    client = cast(SyncLspClient, adapter_runtime.client)
    try:
        deadline.check(f"{spec.name} initialize")
        initialize_result = client.request(
            "initialize", dict(spec.initialize_params(workspace_root)), timeout=deadline.remaining()
        )
        raw_providers = RawLspProviders.from_initialize_result(initialize_result)
        client.notify("initialized", {})
        deadline.check(f"{spec.name} session")
        result = session(client)
        deadline.check(f"{spec.name} session complete")
        return ProtocolSession(raw_providers=raw_providers, engine=spec.engine(runtime), result=result)
    finally:
        shutdown_timeout = max(min(2.0, deadline.remaining()), 0.001)
        with suppress(Exception):
            client.shutdown(timeout=shutdown_timeout)
        provider.stop(adapter_runtime)
