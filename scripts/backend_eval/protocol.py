"""The Phase 2 protocol-plane interface and shared, deadline-bound probe runner.

Decision P2-1 (design.md): the protocol plane starts each locked backend directly through
process-launch and transport primitives -- ``LanguageServerSubprocessLauncher``,
``SubprocessAdapterRuntimeProvider``, ``SyncLspClient`` -- and never constructs a
``LanguageAdapter`` or ``WorkspaceRuntime``. Those own document lifecycle, freshness, lease,
and scope-generation state that belongs to Phase 3's product-seam plane. This module imports
only ``serena_light.lsp.{client,adapter}``, ``serena_light.processes``, and Phase 1's
``scripts.backend_eval.{process,runtime,models}`` -- never ``serena_light.workspace`` -- and
``tests/backend_eval/test_protocol.py`` asserts this by parsing this file's own imports.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.models import DIAGNOSTICS_MODES
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.runtime import CandidateRuntime, minimal_backend_environment
from serena_light.lsp.adapter import (
    EngineMetadata,
    RawLspProviders,
    SubprocessAdapterRuntimeProvider,
    _provider_enabled,  # reuse production's provider-enabled predicate, never reinvented
    _selected_position_encoding,  # reuse production's positionEncoding negotiation, never reinvented
)
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.positions import PositionEncoding
from serena_light.processes import LanguageServerSubprocessLauncher

__all__ = ["BackendProtocolSpec", "ProtocolSession", "run_protocol_probe"]


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
        if self.diagnostics_mode not in DIAGNOSTICS_MODES:
            raise ValueError(f"BackendProtocolSpec.diagnostics_mode must be one of {sorted(DIAGNOSTICS_MODES)}")


@dataclass(frozen=True, slots=True)
class ProtocolSession[T]:
    """The bounded, redacted result of one ``run_protocol_probe`` call.

    This is the one protocol-session representation every later task consumes; no task may
    introduce a second one. Fields beyond ``result`` are the evidence Tasks 2-7 need without
    re-deriving it from a raw transcript: ``raw_providers``/``diagnostic_provider`` and
    ``position_encoding`` are the validated ``initialize`` advertisement (position encoding
    negotiated exactly as production negotiates it), ``stderr_tail`` and ``terminal_errors``
    are the bounded process/transport evidence lifecycle tests need, each already bounded to
    the same 1024-character tail production itself uses for stderr evidence -- never a raw
    unbounded transcript or payload.
    """

    raw_providers: RawLspProviders
    diagnostic_provider: bool
    position_encoding: PositionEncoding
    engine: EngineMetadata
    stderr_tail: str
    terminal_errors: tuple[str, ...]
    result: T


def _child_environment(minimal_env: Mapping[str, str]) -> dict[str, str | None]:
    """Null every ambient variable outside the exact minimal allowlist, then set the rest.

    ``LanguageServerSubprocessLauncher.launch`` merges its ``env`` argument onto a *copy* of
    the current process environment (production needs ambient inheritance for its own
    launches -- see ``pyright.py``/``typescript.py``, which only override ``PATH``/``NODE_PATH``);
    only keys explicitly present in the mapping handed to it are overridden or removed. Without
    explicitly nulling every other ambient key, anything outside ``minimal_backend_environment``'s
    fixed 7-key allowlist -- ``CONDA_PREFIX``, ``PYTHONHOME``, ``LD_PRELOAD``, ``NODE_OPTIONS``,
    ``PIP_*``/``UV_*`` indexes, mixed-case proxy variables, and so on -- would still reach the
    candidate process through that merge, violating the Global Constraints "no ambient PATH, no
    inherited PYTHONPATH, no ``*_PROXY`` variable" contract. This only reads ``os.environ`` and
    returns a new overlay mapping; it never mutates the real process environment.
    """

    overlay: dict[str, str | None] = dict.fromkeys(os.environ, None)
    overlay.update(minimal_env)
    return overlay


def run_protocol_probe[T](
    spec: BackendProtocolSpec,
    runtime: CandidateRuntime,
    workspace_root: Path,
    *,
    deadline: Deadline,
    session: Callable[[SyncLspClient], T],
) -> ProtocolSession[T]:
    """Launch ``spec``'s backend, run ``initialize``/``initialized``, then ``session``.

    ``SubprocessAdapterRuntimeProvider.stop()`` is always called, even when ``session`` raises.
    A ``deadline.expired()`` observation -- whether raised before launch, before ``initialize``,
    before ``session``, or discovered only after ``session`` returns -- is never swallowed by
    cleanup. Cleanup exception precedence: if ``session``/a deadline check raised (a *primary*
    failure already in flight), a subsequent ``provider.stop()`` failure is recorded onto that
    primary exception's notes and suppressed, never replacing it; if there was no primary
    failure, a ``provider.stop()`` failure propagates normally so it is not silently lost.

    If the deadline is already exhausted by the time cleanup starts, the graceful
    ``client.shutdown()`` handshake is skipped entirely rather than attempted with a token
    near-zero timeout: a "graceful" shutdown that cannot possibly complete a real
    request/notify round trip in the time available is not evidence of a clean shutdown, and
    pretending otherwise by attempting it anyway (and quietly suppressing its inevitable
    failure) would be dishonest. Cleanup always still forcefully reaps the process tree via
    ``provider.stop()`` regardless of remaining budget.
    """

    deadline.check(f"{spec.name} protocol probe start")
    command = spec.build_command(runtime)
    env = _child_environment(minimal_backend_environment(runtime, runtime.python))
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
    primary: BaseException | None = None
    try:
        deadline.check(f"{spec.name} initialize")
        initialize_result = client.request(
            "initialize", dict(spec.initialize_params(workspace_root)), timeout=deadline.remaining()
        )
        if not isinstance(initialize_result, Mapping):
            raise TypeError(f"{spec.name} initialize result must be an object")
        raw_providers = RawLspProviders.from_initialize_result(initialize_result)
        position_encoding = _selected_position_encoding(initialize_result, spec.position_encoding)
        capabilities = initialize_result.get("capabilities")
        diagnostic_provider = isinstance(capabilities, Mapping) and _provider_enabled(
            cast("Mapping[str, object]", capabilities).get("diagnosticProvider")
        )
        client.notify("initialized", {})
        deadline.check(f"{spec.name} session")
        result = session(client)
        deadline.check(f"{spec.name} session complete")
        stderr_capture = adapter_runtime.stderr_capture
        stderr_tail = "" if stderr_capture is None else stderr_capture.snapshot().decode("utf-8", "replace")[-1024:]
        return ProtocolSession(
            raw_providers=raw_providers,
            diagnostic_provider=diagnostic_provider,
            position_encoding=position_encoding,
            engine=spec.engine(runtime),
            stderr_tail=stderr_tail,
            terminal_errors=tuple(str(error) for error in terminal_errors),
            result=result,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        remaining = deadline.remaining()
        if remaining > 0:
            with suppress(Exception):
                client.shutdown(timeout=min(2.0, remaining))
        try:
            provider.stop(adapter_runtime)
        except BaseException as stop_error:
            if primary is None:
                raise
            primary.add_note(f"cleanup: SubprocessAdapterRuntimeProvider.stop() also raised: {stop_error!r}")
