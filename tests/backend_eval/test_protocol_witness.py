"""Deterministic behavior-contract tests for the private Phase 2 witness.

The fake runner replaces only the external language-server process.  The witness still
creates, opens, reads, hashes, maps, verifies, and removes its real disposable fixture.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.backend_eval import protocol_witness as witness_module
from scripts.backend_eval.manifests import MS_TRANSFORMERS_ROOT
from scripts.backend_eval.models import EnvironmentIdentity, ServiceConfigIdentity
from scripts.backend_eval.process import Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.protocol import BackendProtocolSpec, ProtocolSession
from scripts.backend_eval.protocol_witness import (
    FIXTURE_BYTES,
    ProtocolWitnessRequest,
    ProtocolWitnessSetupError,
    run_protocol_behavior_witness,
)
from scripts.backend_eval.runtime import SERVICE_CONFIG_RELPATHS, CandidateRuntime
from serena_light.lsp.adapter import EngineMetadata, RawLspProviders
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.positions import PositionEncoding

_INTERPRETER_VERSION = "3.12.11"
_EXTERNAL_DEFINITION = MS_TRANSFORMERS_ROOT / "generation/configuration_utils.py"
_RUN_IDENTITY = "a" * 64


def _owned_run_root(tmp_path: Path) -> Path:
    root = tmp_path / _RUN_IDENTITY
    root.mkdir(mode=0o700)
    return root


class _RecordingDeadline:
    def __init__(self, *, expire_at: str | None = None) -> None:
        self.checks: list[str] = []
        self.expire_at = expire_at
        self.expired_at: str | None = None
        self.error: DeadlineExceeded | None = None

    def check(self, step: str) -> None:
        self.checks.append(step)
        if self.expired_at is not None or step == self.expire_at:
            self.expired_at = self.expired_at or step
            if self.error is None:
                self.error = DeadlineExceeded(f"step={step} injected deadline")
            raise self.error

    def elapsed(self) -> float:
        return 0.25

    def remaining(self) -> float:
        return 30.0 if self.expired_at is None else 0.0


def _runtime(tmp_path: Path) -> CandidateRuntime:
    digest = "1" * 64
    root = tmp_path / "runtime" / digest
    bin_dir = root / "venv/bin"
    home = root / "home"
    cache = root / "cache"
    config = root / "config"
    for directory in (bin_dir, home, cache, config):
        directory.mkdir(parents=True, mode=0o700)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)
    ty = bin_dir / "ty"
    pyrefly = bin_dir / "pyrefly"
    ty.write_bytes(b"")
    pyrefly.write_bytes(b"")
    service_configs: list[ServiceConfigIdentity] = []
    for backend, relative_path in sorted(SERVICE_CONFIG_RELPATHS.items()):
        path = config / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{backend}\n".encode())
        path.chmod(0o600)
        service_configs.append(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(path),
                config_sha256={"pyrefly": "2", "pyright": "3", "ty": "4"}[backend] * 64,
                home_path=str(home),
                cache_path=str(cache),
            )
        )
    manifest = root / "runtime-manifest.json"
    manifest.write_bytes(b"{}")
    return CandidateRuntime(
        root=root,
        python=python,
        ty=ty,
        pyrefly=pyrefly,
        lock_digest=digest,
        executable_hashes=(("pyrefly", "5" * 64), ("ty", "6" * 64)),
        home=home,
        cache=cache,
        config=config,
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path=str(python),
                interpreter_realpath=str(python.resolve()),
                version=_INTERPRETER_VERSION,
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path=str(python),
                interpreter_realpath=str(python.resolve()),
                version=_INTERPRETER_VERSION,
            ),
        ),
        service_configs=tuple(service_configs),
        manifest_path=manifest,
        manifest_sha256="7" * 64,
    )


def _service_config(runtime: CandidateRuntime, candidate: str) -> ServiceConfigIdentity:
    return next(item for item in runtime.service_configs if item.backend == candidate)


def _spec(
    runtime: CandidateRuntime,
    candidate: str,
    *,
    encoding: PositionEncoding = PositionEncoding.UTF16,
) -> BackendProtocolSpec:
    interpreter = next(
        Path(item.interpreter_path) for item in runtime.environments if item.name == "ms"
    )
    config = _service_config(runtime, candidate)

    def initialize_params(root: Path) -> Mapping[str, object]:
        base: dict[str, object] = {"rootUri": root.as_uri(), "capabilities": {}}
        if candidate == "pyrefly":
            base["initializationOptions"] = {
                "pythonPath": str(interpreter),
                "pyrefly": {
                    "configPath": config.config_path,
                    "diagnosticMode": "workspace",
                },
            }
        return base

    request_handlers: Mapping[str, Callable[[Any], Any]] | None = None
    if candidate == "pyright":
        request_handlers = {
            "workspace/configuration": lambda _params: [
                {"pythonPath": str(interpreter)},
                {
                    "diagnosticMode": "workspace",
                    "autoSearchPaths": True,
                    "useLibraryCodeForTypes": True,
                },
                {},
            ]
        }
    elif candidate == "ty":
        request_handlers = {
            "workspace/configuration": lambda _params: [
                {
                    "configurationFile": config.config_path,
                    "configuration": {"environment": {"python": str(interpreter)}},
                }
            ]
        }
    elif candidate == "pyrefly":
        request_handlers = {
            "workspace/configuration": lambda params: [
                initialize_params(Path("/"))["initializationOptions"]
                for _item in cast("Mapping[str, list[object]]", params)["items"]
            ]
        }
    return BackendProtocolSpec(
        name=candidate,
        build_command=lambda candidate_runtime: (str(candidate_runtime.python),),
        initialize_params=initialize_params,
        request_handlers=request_handlers,
        engine=lambda candidate_runtime: EngineMetadata(
            name=candidate,
            version="test",
            executable=candidate_runtime.python,
            interpreter=interpreter,
        ),
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="pull" if candidate == "ty" else "push",
    )


class _FakeClient:
    def __init__(
        self,
        spec: BackendProtocolSpec,
        fixture_uri: str,
        encoding: PositionEncoding,
        *,
        diagnostics_uri: str | None,
        y_raw_start: int | None,
        external_definition: Path | None,
        publish_empty_before_diagnostic: bool,
    ) -> None:
        self._spec = spec
        self._fixture_uri = fixture_uri
        self._encoding = encoding
        self._diagnostics_uri = diagnostics_uri
        self._y_raw_start = y_raw_start
        self._external_definition = external_definition
        self._publish_empty_before_diagnostic = publish_empty_before_diagnostic
        self._definition_count = 0

    def notify(self, method: str, params: object) -> None:
        if method != "textDocument/didOpen" or self._spec.notification_handler is None:
            return
        uri = self._diagnostics_uri or self._fixture_uri
        if self._publish_empty_before_diagnostic:
            self._spec.notification_handler(
                "textDocument/publishDiagnostics",
                {"uri": uri, "diagnostics": []},
            )
        self._spec.notification_handler(
            "textDocument/publishDiagnostics",
            {
                "uri": uri,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 5},
                            "end": {"line": 0, "character": 48},
                        },
                        "severity": 1,
                        "message": (
                            'Import "definitely_missing_serena_light_witness" '
                            "could not be resolved"
                        ),
                    }
                ],
            },
        )

    def request(self, method: str, params: object, *, timeout: float) -> object:
        del params, timeout
        assert method == "textDocument/definition"
        self._definition_count += 1
        if self._definition_count == 1:
            if self._external_definition is None:
                return None
            return {
                "uri": self._external_definition.as_uri(),
                "range": {
                    "start": {"line": 70, "character": 0},
                    "end": {"line": 70, "character": 22},
                },
            }
        raw_start = self._y_raw_start
        if raw_start is None:
            raw_start = {
                PositionEncoding.UTF8: 21,
                PositionEncoding.UTF16: 19,
                PositionEncoding.UTF32: 18,
            }[self._encoding]
        return {
            "uri": self._fixture_uri,
            "range": {
                "start": {"line": 4, "character": raw_start},
                "end": {"line": 4, "character": raw_start + 1},
            },
        }


def _fake_runner(
    *,
    encoding: PositionEncoding = PositionEncoding.UTF16,
    diagnostics_uri: str | None = None,
    mutate_after_session: bool = False,
    y_raw_start: int | None = None,
    terminal_errors: tuple[str, ...] = (),
    cleanup_errors: tuple[str, ...] = (),
    exit_status: int | None = 0,
    swap_directory_after_session: bool = False,
    expected_deadline: Deadline | None = None,
    external_definition: Path | None = _EXTERNAL_DEFINITION,
    publish_empty_before_diagnostic: bool = False,
) -> Callable[..., ProtocolSession[Any]]:
    def run(
        spec: BackendProtocolSpec,
        runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Callable[[SyncLspClient, RawLspProviders], object],
    ) -> ProtocolSession[object]:
        if expected_deadline is not None:
            assert deadline is expected_deadline
        params = dict(spec.initialize_params(workspace_root))
        if spec.validate_initialize_params is not None:
            spec.validate_initialize_params(params)
        configuration = (spec.request_handlers or {}).get("workspace/configuration")
        if configuration is not None:
            if spec.name == "pyright":
                configuration(
                    {
                        "items": [
                            {"section": "python"},
                            {"section": "python.analysis"},
                            {"section": "pyright"},
                        ]
                    }
                )
            elif spec.name == "ty":
                configuration(
                    {
                        "items": [
                            {
                                "scopeUri": workspace_root.as_uri(),
                                "section": "ty",
                            }
                        ]
                    }
                )
            else:
                configuration({"items": [{"section": "pyrefly"}]})
        client = _FakeClient(
            spec,
            (workspace_root / "witness.py").as_uri(),
            encoding,
            diagnostics_uri=diagnostics_uri,
            y_raw_start=y_raw_start,
            external_definition=external_definition,
            publish_empty_before_diagnostic=publish_empty_before_diagnostic,
        )
        result = session(cast("SyncLspClient", client), RawLspProviders(definition=True))
        if mutate_after_session:
            (workspace_root / "witness.py").write_bytes(b"mutated\n")
        if swap_directory_after_session:
            workspace_root.rename(workspace_root.with_name(f"{workspace_root.name}-moved"))
            workspace_root.mkdir(mode=0o700)
        return ProtocolSession(
            raw_providers=RawLspProviders(definition=True),
            diagnostic_provider=False,
            position_encoding=encoding,
            engine=spec.engine(runtime),
            stderr_tail="",
            terminal_errors=terminal_errors,
            cleanup_errors=cleanup_errors,
            exit_status=exit_status,
            result=result,
        )

    return run


def test_witness_rejects_a_shared_root_without_a_run_identity(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    shared_root = tmp_path / "backend-eval"
    shared_root.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="per-run root"):
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=shared_root,
        )


def test_witness_threads_one_deadline_through_every_fixture_io_boundary(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    recording = _RecordingDeadline()
    deadline = cast("Deadline", recording)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=deadline,
        probe_runner=_fake_runner(expected_deadline=deadline),
    )

    assert witness.passed
    assert {
        "protocol witness run root open before",
        "protocol witness run root open after",
        "protocol witness fixture mkdir before",
        "protocol witness fixture mkdir after",
        "protocol witness fixture directory open before",
        "protocol witness fixture directory open after",
        "protocol witness fixture file open before",
        "protocol witness fixture file open after",
        "protocol witness fixture write before",
        "protocol witness fixture write after",
        "protocol witness fixture file fsync before",
        "protocol witness fixture file fsync after",
        "protocol witness verify file open before",
        "protocol witness verify file open after",
        "protocol witness verify read before",
        "protocol witness verify read after",
        "protocol witness cleanup unlink before",
        "protocol witness cleanup unlink after",
        "protocol witness cleanup rmdir before",
        "protocol witness cleanup rmdir after",
    }.issubset(recording.checks)


def test_witness_create_stops_before_write_when_the_same_deadline_expires(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    recording = _RecordingDeadline(expire_at="protocol witness fixture write before")
    launched = False

    def forbidden_runner(*args: object, **kwargs: object) -> ProtocolSession[Any]:
        nonlocal launched
        launched = True
        raise AssertionError((args, kwargs))

    with pytest.raises(DeadlineExceeded, match="fixture write before"):
        run_protocol_behavior_witness(
            ProtocolWitnessRequest(
                candidate="pyright",
                spec=_spec(runtime, "pyright"),
                runtime=runtime,
                owned_root=owned_root,
            ),
            deadline=cast("Deadline", recording),
            probe_runner=forbidden_runner,
        )

    assert not launched
    assert recording.expired_at == "protocol witness fixture write before"
    # No unchecked rollback is allowed after the same deadline expired.  The Task 8 owner
    # can remove this already-owned run root after recording the incomplete phase.
    assert (owned_root / "protocol-witness-pyright/witness.py").is_file()


def test_witness_cleanup_deadline_failure_propagates_and_performs_no_unchecked_unlink(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    recording = _RecordingDeadline(expire_at="protocol witness cleanup unlink before")

    with pytest.raises(DeadlineExceeded, match="cleanup unlink before") as caught:
        run_protocol_behavior_witness(
            ProtocolWitnessRequest(
                candidate="pyright",
                spec=_spec(runtime, "pyright"),
                runtime=runtime,
                owned_root=owned_root,
            ),
            deadline=cast("Deadline", recording),
            probe_runner=_fake_runner(),
        )

    assert caught.value is recording.error
    assert (owned_root / "protocol-witness-pyright/witness.py").is_file()
    assert "protocol witness cleanup unlink after" not in recording.checks
    assert not any("finalization" in step for step in recording.checks)


def test_witness_propagates_the_exact_probe_deadline_and_does_not_mask_it_in_cleanup(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    recording = _RecordingDeadline(expire_at="protocol witness injected probe timeout")

    def timed_out_runner(
        spec: BackendProtocolSpec,
        runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Callable[[SyncLspClient, RawLspProviders], object],
    ) -> ProtocolSession[object]:
        del spec, runtime, workspace_root, session
        deadline.check("protocol witness injected probe timeout")
        raise AssertionError("deadline check must raise")

    with pytest.raises(DeadlineExceeded, match="injected probe timeout") as caught:
        run_protocol_behavior_witness(
            ProtocolWitnessRequest(
                candidate="pyright",
                spec=_spec(runtime, "pyright"),
                runtime=runtime,
                owned_root=owned_root,
            ),
            deadline=cast("Deadline", recording),
            probe_runner=timed_out_runner,
        )

    assert caught.value is recording.error
    assert (owned_root / "protocol-witness-pyright/witness.py").is_file()
    assert any("cleanup" in note for note in getattr(caught.value, "__notes__", ()))


def test_witness_keeps_a_bounded_non_deadline_protocol_failure_as_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    def failed_runner(*args: object, **kwargs: object) -> ProtocolSession[Any]:
        del args, kwargs
        raise RuntimeError("bounded backend failure")

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=failed_runner,
    )

    assert not witness.passed
    assert any("RuntimeError" in issue for issue in witness.issues)
    assert not tuple(owned_root.iterdir())


@pytest.mark.parametrize("candidate", ["pyright", "ty", "pyrefly"])
def test_witness_records_exact_candidate_configuration_transport(
    tmp_path: Path,
    candidate: str,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate=candidate,
            spec=_spec(runtime, candidate),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(),
    )

    assert witness.passed
    assert witness.selected_interpreter == str(runtime.python)
    assert witness.configuration_interpreter == str(runtime.python)
    assert witness.configuration_transport == {
        "pyright": "workspace_configuration",
        "ty": "workspace_configuration",
        "pyrefly": "initialization_options",
    }[candidate]
    assert witness.configuration_path == (
        None if candidate == "pyright" else _service_config(runtime, candidate).config_path
    )
    assert witness.configuration_payload_sha256 is not None
    assert witness.configuration_request_count == 1
    assert witness.configuration_application_proven
    assert not tuple(owned_root.iterdir())


@pytest.mark.parametrize(
    ("encoding", "raw_start"),
    [
        (PositionEncoding.UTF8, 21),
        (PositionEncoding.UTF16, 19),
        (PositionEncoding.UTF32, 18),
    ],
)
def test_witness_maps_the_server_returned_non_bmp_y_range_from_negotiated_encoding(
    tmp_path: Path,
    encoding: PositionEncoding,
    raw_start: int,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(encoding=encoding),
    )

    assert witness.passed
    assert witness.position_encoding == encoding.value
    assert witness.y_raw_range == (4, raw_start, 4, raw_start + 1)
    assert witness.y_decoded_range == (4, 18, 4, 19)


def test_witness_binds_external_definition_diagnostics_and_first_readiness(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(publish_empty_before_diagnostic=True),
    )

    assert witness.external_definition_relative_path == "generation/configuration_utils.py"
    assert witness.push_diagnostics_claimed
    assert witness.exact_uri_diagnostics
    assert witness.missing_import_diagnostic
    assert witness.exact_uri_publish_count == 2
    assert witness.exact_uri_diagnostic_count == 1
    assert witness.diagnostics_completion_reason == "missing_import_observed"
    assert witness.first_normalized_capability == "definition"
    assert witness.first_normalized_count == 1
    assert witness.first_readiness_seconds is not None
    assert witness.first_readiness_seconds >= 0.0
    assert witness.fixture_unchanged
    assert witness.fixture_sha256 == "84abcaab9124d982101995bc01a6119083c9a7c15de158e31015ee266a91ff40"
    assert witness.fixture_mode == 0o600
    assert witness.issues == ()


def test_empty_exact_uri_publish_does_not_complete_the_missing_import_wait() -> None:
    observation = witness_module._DiagnosticsObservation("file:///fixture.py")

    observation.observe(
        "textDocument/publishDiagnostics",
        {"uri": "file:///fixture.py", "diagnostics": []},
    )

    assert observation.exact_uri_observed
    assert observation.exact_uri_count == 1
    assert observation.diagnostic_count == 0
    assert not observation.missing_import_observed
    assert observation.event is not None
    assert not observation.event.is_set()

    observation.observe(
        "textDocument/publishDiagnostics",
        {
            "uri": "file:///fixture.py",
            "diagnostics": [{"message": "definitely_missing_serena_light_witness"}],
        },
    )

    assert observation.exact_uri_count == 2
    assert observation.diagnostic_count == 1
    assert observation.missing_import_observed
    assert observation.event.is_set()


def test_ty_configuration_remains_inconclusive_without_behavioral_application_proof(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="ty",
            spec=_spec(runtime, "ty"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(external_definition=None),
    )

    assert not witness.configuration_application_proven
    assert not witness.passed
    assert any("configuration application" in issue for issue in witness.issues)


def test_ty_configuration_remains_inconclusive_without_a_server_request(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    sent_only_spec = replace(_spec(runtime, "ty"), request_handlers=None)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="ty",
            spec=sent_only_spec,
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(),
    )

    assert witness.configuration_request_count == 0
    assert not witness.configuration_application_proven
    assert not witness.passed
    assert any("server request" in issue for issue in witness.issues)


def test_witness_fails_closed_on_wrong_diagnostics_uri_and_fixture_mutation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(
            diagnostics_uri=(owned_root / "wrong.py").as_uri(),
            mutate_after_session=True,
        ),
    )

    assert not witness.passed
    assert not witness.exact_uri_diagnostics
    assert not witness.missing_import_diagnostic
    assert not witness.fixture_unchanged
    assert any("exact fixture URI" in issue for issue in witness.issues)
    assert any("fixture changed" in issue for issue in witness.issues)
    assert not tuple(owned_root.iterdir())


def test_witness_returns_a_typed_issue_when_the_server_range_splits_the_non_bmp_marker(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        # UTF-16 column 15 is inside the astral marker's two-code-unit span.
        probe_runner=_fake_runner(y_raw_start=15),
    )

    assert not witness.passed
    assert witness.y_raw_range == (4, 15, 4, 16)
    assert witness.y_decoded_range is None
    assert any("position" in issue for issue in witness.issues)
    assert not tuple(owned_root.iterdir())


def test_witness_cannot_pass_with_terminal_cleanup_or_nonzero_exit_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(
            terminal_errors=("transport closed",),
            cleanup_errors=("stop failed",),
            exit_status=2,
        ),
    )

    assert not witness.passed
    assert witness.terminal_error_count == 1
    assert witness.cleanup_error_count == 1
    assert any("terminal" in issue for issue in witness.issues)
    assert any("cleanup" in issue for issue in witness.issues)
    assert any("status 2" in issue for issue in witness.issues)


def test_witness_refuses_to_overwrite_a_previous_fixture(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    fixture_root = owned_root / "protocol-witness-pyright"
    fixture_root.mkdir(parents=True)
    existing = fixture_root / "witness.py"
    existing.write_bytes(b"owned by another run\n")

    with pytest.raises(ProtocolWitnessSetupError, match="already exists"):
        run_protocol_behavior_witness(
            ProtocolWitnessRequest(
                candidate="pyright",
                spec=_spec(runtime, "pyright"),
                runtime=runtime,
                owned_root=owned_root,
            ),
            deadline=Deadline.start(monotonic_clock, 30.0),
            probe_runner=_fake_runner(),
        )

    assert existing.read_bytes() == b"owned by another run\n"


def test_witness_setup_failure_removes_only_its_partially_created_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    def fail_write(_fd: int, _payload: object) -> int:
        raise OSError("injected fixture write failure")

    monkeypatch.setattr(witness_module.os, "write", fail_write)
    with pytest.raises(ProtocolWitnessSetupError, match="create disposable fixture"):
        run_protocol_behavior_witness(
            ProtocolWitnessRequest(
                candidate="pyright",
                spec=_spec(runtime, "pyright"),
                runtime=runtime,
                owned_root=owned_root,
            ),
            deadline=Deadline.start(monotonic_clock, 30.0),
            probe_runner=_fake_runner(),
        )

    assert not tuple(owned_root.iterdir())


def test_witness_cleanup_never_removes_a_swapped_in_directory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=_spec(runtime, "pyright"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(swap_directory_after_session=True),
    )

    replacement = owned_root / "protocol-witness-pyright"
    moved = owned_root / "protocol-witness-pyright-moved"
    assert not witness.passed
    assert replacement.is_dir()
    assert moved.is_dir()
    assert not tuple(moved.iterdir())
    assert any("identity" in issue for issue in witness.issues)


def test_witness_sidecar_is_canonical_bounded_and_contains_no_fixture_source(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)
    witness = run_protocol_behavior_witness(
        ProtocolWitnessRequest(
            candidate="ty",
            spec=_spec(runtime, "ty"),
            runtime=runtime,
            owned_root=owned_root,
        ),
        deadline=Deadline.start(monotonic_clock, 30.0),
        probe_runner=_fake_runner(),
    )

    payload = witness.canonical_bytes()
    assert payload == json.dumps(
        witness.to_dict(),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode() + b"\n"
    assert len(payload) < 8_192
    assert FIXTURE_BYTES not in payload
    assert b"definitely_missing_serena_light_witness" not in payload


def test_witness_rejects_a_spec_for_another_candidate_before_writing(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    owned_root = _owned_run_root(tmp_path)

    with pytest.raises(ValueError, match="spec.name"):
        ProtocolWitnessRequest(
            candidate="pyright",
            spec=replace(_spec(runtime, "pyright"), name="ty"),
            runtime=runtime,
            owned_root=owned_root,
        )

    assert not tuple(owned_root.iterdir())
