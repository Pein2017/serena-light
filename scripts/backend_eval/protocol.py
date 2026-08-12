"""The Phase 2 protocol-plane interface and shared, deadline-bound probe runner.

Decision P2-1 (design.md): the protocol plane starts each locked backend directly through
process-launch and transport primitives -- ``LanguageServerSubprocessLauncher``,
``SubprocessAdapterRuntimeProvider``, ``SyncLspClient`` -- and never constructs a
``LanguageAdapter`` or ``WorkspaceRuntime``. Those own document lifecycle, freshness, lease,
and scope-generation state that belongs to Phase 3's product-seam plane. This module imports
only ``serena_light.{lsp.{client,adapter},debug_logging}``, ``serena_light.processes``, and
Phase 1's ``scripts.backend_eval.{process,runtime,models}`` -- never ``serena_light.workspace``
-- and ``tests/backend_eval/test_protocol.py`` asserts this by parsing this file's own imports.
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
from serena_light.debug_logging import _redact  # reuse production's own secret redaction, never a copy
from serena_light.lsp.adapter import (
    AdapterRuntime,
    BoundedStderrCapture,
    EngineMetadata,
    RawLspProviders,
    SubprocessAdapterRuntimeProvider,
    _provider_enabled,  # reuse production's provider-enabled predicate, never reinvented
    _selected_position_encoding,  # reuse production's positionEncoding negotiation, never reinvented
)
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.positions import PositionEncoding
from serena_light.processes import LanguageServerSubprocessLauncher

__all__ = ["BackendProtocolSpec", "ProtocolSession", "protocol_session_from_error", "run_protocol_probe"]

# Below this many seconds remaining, a graceful client.shutdown() round trip cannot possibly
# complete honestly; the handshake is skipped entirely rather than attempted with a
# near-zero timeout that can only fail (Minor 4; see also the M10 note in run_protocol_probe).
_GRACEFUL_SHUTDOWN_MINIMUM_SECONDS = 0.05

_PROTOCOL_SESSION_EVIDENCE_ATTR = "protocol_session_evidence"


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
    """The bounded, redacted evidence of one ``run_protocol_probe`` call.

    This is the one protocol-session representation every later task consumes, on **both**
    success and failure -- no task may introduce a second one, and there is no separate
    failure-evidence type. On success it is returned directly; on failure, ``run_protocol_probe``
    still raises its own original typed exception (a ``DeadlineExceeded``, an
    ``LspProcessLost``, whatever ``session`` itself raised), but attaches this same shape to
    it with ``result=None``, retrievable via :func:`protocol_session_from_error`.

    ``raw_providers``/``diagnostic_provider`` and ``position_encoding`` are the validated
    ``initialize`` advertisement (position encoding negotiated exactly as production
    negotiates it; all default/unset if the failure happened before ``initialize``
    completed). ``stderr_tail``, candidate ``terminal_errors``, harness ``cleanup_errors``,
    and the candidate's post-stop ``exit_status`` are captured only *after* cleanup
    (``client.shutdown()`` and ``provider.stop()``) has finished, so they reflect the
    process's full lifetime without forcing consumers to parse string prefixes to distinguish
    candidate failures from harness cleanup failures. ``stderr_tail`` is redacted with
    production's own secret redaction before being bounded to its last 1024 characters --
    never a raw unbounded transcript or payload.
    """

    raw_providers: RawLspProviders
    diagnostic_provider: bool
    position_encoding: PositionEncoding
    engine: EngineMetadata
    stderr_tail: str
    terminal_errors: tuple[str, ...]
    cleanup_errors: tuple[str, ...]
    exit_status: int | None
    result: T


def protocol_session_from_error(error: BaseException) -> ProtocolSession[Any] | None:
    """The :class:`ProtocolSession` evidence ``run_protocol_probe`` attached to a failure.

    Returns ``None`` for any exception not raised by ``run_protocol_probe`` (or one it never
    reached the point of attaching evidence to, which does not happen in practice since
    evidence is attached in every failure path).
    """

    return getattr(error, _PROTOCOL_SESSION_EVIDENCE_ATTR, None)


def _attach_protocol_session(error: BaseException, session_evidence: ProtocolSession[Any]) -> None:
    setattr(error, _PROTOCOL_SESSION_EVIDENCE_ATTR, session_evidence)


def _try_attach_protocol_session(error: BaseException, session_evidence: ProtocolSession[Any]) -> None:
    """Attach evidence to ``error`` without ever letting the attempt replace it.

    ``setattr`` on an ordinary exception instance always succeeds (every exception this
    module raises or lets propagate has a normal ``__dict__``), but if some future or
    caller-supplied exception type refuses arbitrary attributes (for example, one declaring
    ``__slots__`` with no ``__dict__``), attachment must fail closed onto the *same* original
    exception -- preserving its type and message -- rather than let that failure become an
    unrelated new exception that silently replaces it. A best-effort, bounded note records
    that evidence attachment failed; if even that cannot be recorded, it is dropped rather
    than risk masking ``error`` itself.
    """

    try:
        _attach_protocol_session(error, session_evidence)
    except BaseException as attach_error:
        with suppress(BaseException):
            error.add_note(f"cleanup: could not attach protocol session evidence: {attach_error!r}")


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


def _redacted_stderr_tail(stderr_capture: BoundedStderrCapture | None) -> str:
    if stderr_capture is None:
        return ""
    text = stderr_capture.snapshot().decode("utf-8", "replace")
    return _redact(text)[-1024:]


def _cleanup(
    client: SyncLspClient,
    provider: SubprocessAdapterRuntimeProvider,
    adapter_runtime: AdapterRuntime,
    deadline: Deadline,
    primary: BaseException | None,
) -> tuple[BaseException | None, tuple[str, ...]]:
    """Run the graceful shutdown and forceful stop, honestly, without losing a failure.

    A graceful ``client.shutdown()`` is attempted only when there is a meaningfully bounded
    remaining budget for it (Minor 4); an already-exhausted or near-exhausted budget skips
    the handshake entirely rather than pretending a token near-zero attempt is evidence of a
    clean shutdown. ``provider.stop()`` always still runs regardless of remaining budget or
    of whether the shutdown handshake ran or failed.

    Neither failure is ever silently dropped: each is recorded into the returned typed
    ``cleanup_errors`` tuple, separate from candidate-process ``terminal_errors``, and each
    follows the identical primary-exception precedence rule -- if there is already a primary
    failure in flight, this failure is recorded onto its notes and suppressed; if there is
    none, this failure itself becomes the primary the caller raises, and cleanup still
    continues (``provider.stop()`` still runs even when ``client.shutdown()`` just became the
    primary failure).
    """

    cleanup_errors: list[str] = []
    remaining = deadline.remaining()
    if remaining >= _GRACEFUL_SHUTDOWN_MINIMUM_SECONDS:
        # Mirror SubprocessAdapterRuntimeProvider.stop()'s own convention: mark this runtime
        # as intentionally stopping *before* the shutdown call, so the client's own resulting
        # transport-closed notification is not misreported as a terminal process error --
        # it is an expected consequence of the graceful shutdown we are ourselves initiating.
        if adapter_runtime.stopping is not None:
            adapter_runtime.stopping.set()
        try:
            client.shutdown(timeout=min(2.0, remaining))
        except BaseException as shutdown_error:
            cleanup_errors.append(f"client.shutdown() raised: {shutdown_error!r}")
            if primary is None:
                primary = shutdown_error
            else:
                primary.add_note(f"cleanup: client.shutdown() also raised: {shutdown_error!r}")
    try:
        provider.stop(adapter_runtime)
    except BaseException as stop_error:
        cleanup_errors.append(f"SubprocessAdapterRuntimeProvider.stop() raised: {stop_error!r}")
        if primary is None:
            primary = stop_error
        else:
            primary.add_note(f"cleanup: SubprocessAdapterRuntimeProvider.stop() also raised: {stop_error!r}")
    return primary, tuple(cleanup_errors)


def run_protocol_probe[T](
    spec: BackendProtocolSpec,
    runtime: CandidateRuntime,
    workspace_root: Path,
    *,
    deadline: Deadline,
    session: Callable[[SyncLspClient], T],
) -> ProtocolSession[T]:
    """Launch ``spec``'s backend, run ``initialize``/``initialized``, then ``session``.

    Cleanup (``client.shutdown()``, ``SubprocessAdapterRuntimeProvider.stop()``) always runs,
    even when ``session`` raises, even when a deadline check raises before the process is
    even launched. A ``deadline.expired()`` observation -- whether raised before launch,
    before ``initialize``, before ``session``, or discovered only after ``session`` returns
    -- is never swallowed by cleanup: ``run_protocol_probe`` always re-raises its own
    original typed exception (never a second wrapper representation), and cleanup failures
    are folded into that same precedence via :func:`_cleanup` rather than replacing it.

    On any failure, the same bounded :class:`ProtocolSession` evidence a successful call
    would have returned is attached to the raised exception (``result=None``), retrievable
    via :func:`protocol_session_from_error` -- so later lifecycle tasks never need a second,
    failure-only evidence type. ``spec.engine(runtime)`` is evaluated first, before any
    process side effect: it needs only ``runtime``, so a failing engine callable starts no
    child and -- because it happens before ``provider``/``client`` exist -- is simply the one
    failure raised, never a `finally`-block failure that could replace or mask a different
    primary failure or leave cleanup evidence half-built.
    """

    raw_providers = RawLspProviders()
    diagnostic_provider = False
    position_encoding = spec.position_encoding
    engine: EngineMetadata | None = None
    result: T | None = None
    terminal_errors: list[BaseException] = []
    terminal_error_strings: tuple[str, ...] = ()
    cleanup_error_strings: tuple[str, ...] = ()
    exit_status: int | None = None
    stderr_tail = ""
    primary: BaseException | None = None
    provider: SubprocessAdapterRuntimeProvider | None = None
    adapter_runtime: AdapterRuntime | None = None
    client: SyncLspClient | None = None
    session_evidence: ProtocolSession[Any] | None = None
    try:
        deadline.check(f"{spec.name} protocol probe start")
        engine = spec.engine(runtime)
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
        adapter_runtime = provider.start(
            notification_handler=lambda _method, _params: None,
            terminal_handler=terminal_errors.append,
        )
        # SubprocessAdapterRuntimeProvider.start always constructs a real SyncLspClient; its
        # return type widens to the AdapterClient protocol for adapter-neutral callers.
        client = cast(SyncLspClient, adapter_runtime.client)
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
    except BaseException as error:
        primary = error
    finally:
        if client is not None and provider is not None and adapter_runtime is not None:
            primary, cleanup_error_strings = _cleanup(client, provider, adapter_runtime, deadline, primary)
            stderr_tail = _redacted_stderr_tail(adapter_runtime.stderr_capture)
            terminal_error_strings = tuple(str(error) for error in terminal_errors)
            if adapter_runtime.process is not None:
                exit_status = adapter_runtime.process.poll()
        if engine is not None:
            session_evidence = ProtocolSession(
                raw_providers=raw_providers,
                diagnostic_provider=diagnostic_provider,
                position_encoding=position_encoding,
                engine=engine,
                stderr_tail=stderr_tail,
                terminal_errors=terminal_error_strings,
                cleanup_errors=cleanup_error_strings,
                exit_status=exit_status,
                result=None if primary is not None else result,
            )
            if primary is not None:
                _try_attach_protocol_session(primary, session_evidence)

    if primary is not None:
        raise primary
    assert session_evidence is not None  # engine (and everything else) succeeded on this path
    return cast("ProtocolSession[T]", session_evidence)
