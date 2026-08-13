"""Locked-Pyrefly protocol-plane facts, capability behavior, and write isolation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.backend_eval.models import EnvironmentIdentity, ServiceConfigIdentity
from scripts.backend_eval.process import Deadline, DeadlineExceeded
from scripts.backend_eval.protocol import ProtocolSession
from scripts.backend_eval.pyrefly_probe import (
    PyreflyWorkspaceMutation,
    pyrefly_protocol_spec,
    run_pyrefly_capability_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime, minimal_backend_environment
from serena_light.lsp.adapter import RawLspProviders
from serena_light.lsp.client import LspResponseError
from serena_light.lsp.positions import PositionEncoding

pytestmark = pytest.mark.timeout(30)

_LOCK_DIGEST = "a" * 64
_MANIFEST_DIGEST = "b" * 64
_CONFIG_DIGEST = "c" * 64


@dataclass
class _FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeClient:
    def __init__(
        self,
        clock: _FakeClock,
        responses: Mapping[str, object],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.clock = clock
        self.responses = responses
        self.close_error = close_error
        self.requests: list[tuple[str, object, float | None]] = []
        self.notifications: list[tuple[str, object]] = []
        self.notification_handler: Any = None
        self.push_diagnostics_observed = True
        self.foreign_push_diagnostics_uri = False
        self.empty_push_diagnostics = False
        self.push_diagnostics_version: int | None = 1
        self._pending_diagnostics: Mapping[str, object] | None = None

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        self.requests.append((method, params, timeout))
        if self._pending_diagnostics is not None and self.notification_handler is not None:
            pending = self._pending_diagnostics
            self._pending_diagnostics = None
            self.notification_handler("textDocument/publishDiagnostics", pending)
        self.clock.advance(0.1)
        response = self.responses[method]
        if isinstance(response, BaseException):
            raise response
        return response

    def notify(self, method: str, params: object = None) -> None:
        self.notifications.append((method, params))
        if (
            method == "textDocument/didOpen"
            and self.push_diagnostics_observed
            and self.notification_handler is not None
        ):
            source_uri = cast(Any, params)["textDocument"]["uri"]
            if self.foreign_push_diagnostics_uri:
                source_uri = "file:///foreign.py"
            publication: dict[str, object] = {
                "uri": source_uri,
                "diagnostics": (
                    []
                    if self.empty_push_diagnostics
                    else [
                        {
                            "message": "controlled Pyrefly diagnostic",
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 1},
                            },
                        }
                    ]
                ),
            }
            if self.push_diagnostics_version is not None:
                publication["version"] = self.push_diagnostics_version
            # A target publication racing before didOpen returns is stale by contract.
            self.notification_handler("textDocument/publishDiagnostics", publication)
            self._pending_diagnostics = publication
        if method == "textDocument/didClose" and self.close_error is not None:
            raise self.close_error


def _runtime(tmp_path: Path) -> tuple[CandidateRuntime, ServiceConfigIdentity, Path]:
    root = tmp_path / _LOCK_DIGEST
    config_root = root / "config"
    ms_interpreter = tmp_path / "ms/bin/python"
    pyrefly_config = ServiceConfigIdentity(
        backend="pyrefly",
        config_path=str(config_root / "pyrefly/pyrefly.toml"),
        config_sha256=_CONFIG_DIGEST,
        home_path=str(root / "home"),
        cache_path=str(root / "cache"),
    )
    runtime = CandidateRuntime(
        root=root,
        python=root / "venv/bin/python",
        ty=root / "venv/bin/ty",
        pyrefly=root / "venv/bin/pyrefly",
        lock_digest=_LOCK_DIGEST,
        executable_hashes=(("pyrefly", "d" * 64), ("ty", "e" * 64)),
        home=root / "home",
        cache=root / "cache",
        config=config_root,
        environments=(
            EnvironmentIdentity(
                name="ms",
                interpreter_path=str(ms_interpreter),
                interpreter_realpath=str(ms_interpreter),
                version="3.12.11",
            ),
        ),
        service_configs=(
            pyrefly_config,
            ServiceConfigIdentity(
                backend="pyright",
                config_path=str(config_root / "pyright/pyrightconfig.json"),
                config_sha256="f" * 64,
                home_path=str(root / "home"),
                cache_path=str(root / "cache"),
            ),
            ServiceConfigIdentity(
                backend="ty",
                config_path=str(config_root / "ty/ty.toml"),
                config_sha256="1" * 64,
                home_path=str(root / "home"),
                cache_path=str(root / "cache"),
            ),
        ),
        manifest_path=root / "runtime-manifest.json",
        manifest_sha256=_MANIFEST_DIGEST,
    )
    return runtime, pyrefly_config, ms_interpreter


def _responses(target: Path) -> dict[str, object]:
    location = {
        "uri": target.as_uri(),
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        },
    }
    return {
        "textDocument/definition": location,
        "textDocument/references": [location],
        "textDocument/implementation": [location],
        "textDocument/documentSymbol": [
            {
                "name": "Known",
                "kind": 12,
                "range": location["range"],
                "selectionRange": location["range"],
            }
        ],
        "workspace/symbol": [{"name": "Known", "kind": 12, "location": location}],
    }


def _providers(**overrides: bool) -> RawLspProviders:
    fields = {
        "definition": True,
        "implementation": True,
        "references": True,
        "document_symbols": True,
        "workspace_symbols": True,
    }
    fields.update(overrides)
    return RawLspProviders(**fields)


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    captured: dict[str, object],
    *,
    providers: RawLspProviders | None = None,
    diagnostic_provider: bool = False,
    exit_status: int | None = 0,
    manifests: tuple[object, object] = ("stable", "stable"),
    after_protocol_advance: float = 0.0,
    push_diagnostics_observed: bool = True,
    foreign_push_diagnostics_uri: bool = False,
    empty_push_diagnostics: bool = False,
    push_diagnostics_version: int | None = 1,
) -> None:
    import scripts.backend_eval.pyrefly_probe as module

    monkeypatch.setattr(module, "_PUSH_DIAGNOSTICS_WAIT_SECONDS", 0.0)
    manifest_values = iter(manifests)

    def fake_capture(workspace_root: Path, deadline: Deadline) -> object:
        captured.setdefault("manifest_roots", []).append(workspace_root)  # type: ignore[union-attr]
        captured.setdefault("manifest_deadlines", []).append(deadline)  # type: ignore[union-attr]
        deadline.check("fake Pyrefly workspace manifest")
        value = next(manifest_values)
        if isinstance(value, BaseException):
            raise value
        return value

    def fake_run_protocol_probe(
        spec: object,
        runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Any,
    ) -> ProtocolSession[object]:
        validated_providers = providers or _providers()
        captured.update(
            spec=spec,
            runtime=runtime,
            workspace_root=workspace_root,
            deadline=deadline,
            session_providers=validated_providers,
        )
        typed_spec = cast(Any, spec)
        client.notification_handler = typed_spec.notification_handler
        client.push_diagnostics_observed = push_diagnostics_observed
        client.foreign_push_diagnostics_uri = foreign_push_diagnostics_uri
        client.empty_push_diagnostics = empty_push_diagnostics
        client.push_diagnostics_version = push_diagnostics_version
        result = session(client, validated_providers)
        client.clock.advance(after_protocol_advance)
        return ProtocolSession(
            raw_providers=validated_providers,
            diagnostic_provider=diagnostic_provider,
            position_encoding=typed_spec.position_encoding,
            engine=typed_spec.engine(runtime),
            stderr_tail="",
            terminal_errors=(),
            cleanup_errors=(),
            exit_status=exit_status,
            result=result,
        )

    monkeypatch.setattr(module, "_capture_workspace_manifest", fake_capture)
    monkeypatch.setattr(module, "run_protocol_probe", fake_run_protocol_probe)


def _run_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: Mapping[str, object] | None = None,
    providers: RawLspProviders | None = None,
    diagnostic_provider: bool = False,
    close_error: BaseException | None = None,
    manifests: tuple[object, object] = ("stable", "stable"),
    reserve: float = 1.0,
    after_protocol_advance: float = 0.0,
    push_diagnostics_observed: bool = True,
    foreign_push_diagnostics_uri: bool = False,
    empty_push_diagnostics: bool = False,
    push_diagnostics_version: int | None = 1,
) -> tuple[object, _FakeClient, CandidateRuntime, ServiceConfigIdentity, dict[str, object]]:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    clock = _FakeClock()
    client = _FakeClient(clock, responses or _responses(target), close_error=close_error)
    runtime, config, _interpreter = _runtime(tmp_path)
    captured: dict[str, object] = {}
    _install_fake_runner(
        monkeypatch,
        client,
        captured,
        providers=providers,
        diagnostic_provider=diagnostic_provider,
        manifests=manifests,
        after_protocol_advance=after_protocol_advance,
        push_diagnostics_observed=push_diagnostics_observed,
        foreign_push_diagnostics_uri=foreign_push_diagnostics_uri,
        empty_push_diagnostics=empty_push_diagnostics,
        push_diagnostics_version=push_diagnostics_version,
    )
    outcome = run_pyrefly_capability_probe(
        runtime,
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(clock, 10.0, reserve=reserve),
    )
    return outcome, client, runtime, config, captured


def test_pyrefly_protocol_spec_binds_locked_command_external_config_and_ms_interpreter(
    tmp_path: Path,
) -> None:
    runtime, config, ms_interpreter = _runtime(tmp_path)

    spec = pyrefly_protocol_spec(runtime, config)

    assert spec.build_command(runtime) == (
        str(runtime.pyrefly),
        "lsp",
        "--indexing-mode",
        "lazy-blocking",
        "--threads",
        "1",
        "--workspace-indexing-limit",
        "2000",
    )
    assert spec.request_handlers is not None
    assert spec.position_encoding is PositionEncoding.UTF16
    assert spec.diagnostics_mode == "push"
    assert spec.engine(runtime).name == "pyrefly"
    assert spec.engine(runtime).version == "1.2.0"
    assert spec.engine(runtime).executable == runtime.pyrefly
    assert spec.engine(runtime).interpreter == ms_interpreter
    with pytest.raises(ValueError, match="exact caller-bound runtime"):
        spec.build_command(cast(CandidateRuntime, object()))
    with pytest.raises(ValueError, match="exact caller-bound runtime"):
        spec.engine(cast(CandidateRuntime, object()))
    params = spec.initialize_params(tmp_path)
    expected_options = {
        "pythonPath": str(ms_interpreter),
        "pyrefly": {
            "configPath": config.config_path,
            "diagnosticMode": "workspace",
        },
    }
    assert params["rootPath"] == str(tmp_path)
    assert params["rootUri"] == tmp_path.as_uri()
    assert params["initializationOptions"] == expected_options
    assert spec.validate_initialize_params is not None
    assert spec.validate_initialize_params(params) is None
    text_document = cast(Any, params)["capabilities"]["textDocument"]
    assert "linkSupport" not in text_document["definition"]
    assert "linkSupport" not in text_document["implementation"]
    assert spec.request_handlers["workspace/configuration"](
        {"items": [{"scopeUri": tmp_path.as_uri()}, {"section": "python.pyrefly"}]}
    ) == [expected_options, expected_options]
    environment = minimal_backend_environment(runtime, ms_interpreter)
    assert environment["SERENA_LIGHT_SELECTED_PYTHON"] == str(ms_interpreter)
    assert environment["XDG_CONFIG_HOME"] == str(runtime.config)
    assert not any(key.upper().endswith("_PROXY") for key in environment)


def test_pyrefly_initialize_params_validator_rejects_missing_foreign_and_malformed_values(
    tmp_path: Path,
) -> None:
    """Removing any exact interpreter/config/mode check must admit at least one invalid
    initialization object that could silently evaluate a different Python environment."""

    runtime, config, ms_interpreter = _runtime(tmp_path)
    validator = pyrefly_protocol_spec(runtime, config).validate_initialize_params
    assert validator is not None
    valid_options = {
        "pythonPath": str(ms_interpreter),
        "pyrefly": {
            "configPath": config.config_path,
            "diagnosticMode": "workspace",
        },
    }
    invalid_params: tuple[Mapping[str, object], ...] = (
        {},
        {"initializationOptions": "malformed"},
        {"initializationOptions": {"pyrefly": valid_options["pyrefly"]}},
        {
            "initializationOptions": {
                "pythonPath": "/foreign/bin/python",
                "pyrefly": valid_options["pyrefly"],
            }
        },
        {
            "initializationOptions": {
                "pythonPath": str(ms_interpreter),
                "pyrefly": "malformed",
            }
        },
        {
            "initializationOptions": {
                "pythonPath": str(ms_interpreter),
                "pyrefly": {"diagnosticMode": "workspace"},
            }
        },
        {
            "initializationOptions": {
                "pythonPath": str(ms_interpreter),
                "pyrefly": {
                    "configPath": "/foreign/pyrefly.toml",
                    "diagnosticMode": "workspace",
                },
            }
        },
        {
            "initializationOptions": {
                "pythonPath": str(ms_interpreter),
                "pyrefly": {"configPath": config.config_path},
            }
        },
        {
            "initializationOptions": {
                "pythonPath": str(ms_interpreter),
                "pyrefly": {
                    "configPath": config.config_path,
                    "diagnosticMode": "openFilesOnly",
                },
            }
        },
    )

    for params in invalid_params:
        with pytest.raises(ValueError, match="Pyrefly initialize params"):
            validator(params)


@pytest.mark.parametrize(
    "bad_config",
    [
        ServiceConfigIdentity(
            backend="pyrefly",
            config_path="/foreign/pyrefly.toml",
            config_sha256=_CONFIG_DIGEST,
            home_path="/foreign/home",
            cache_path="/foreign/cache",
        ),
        ServiceConfigIdentity(
            backend="ty",
            config_path="/foreign/ty.toml",
            config_sha256=_CONFIG_DIGEST,
            home_path="/foreign/home",
            cache_path="/foreign/cache",
        ),
    ],
)
def test_pyrefly_protocol_spec_rejects_configuration_not_bound_to_runtime(
    tmp_path: Path, bad_config: ServiceConfigIdentity
) -> None:
    runtime, _config, _interpreter = _runtime(tmp_path)

    with pytest.raises(ValueError, match="service-owned Pyrefly configuration"):
        pyrefly_protocol_spec(runtime, bad_config)


@pytest.mark.parametrize(
    "params",
    [None, {"items": None}, {"items": "bad"}, {"items": ["bad"]}],
)
def test_pyrefly_workspace_configuration_rejects_malformed_requests(
    tmp_path: Path, params: object
) -> None:
    runtime, config, _interpreter = _runtime(tmp_path)
    handlers = pyrefly_protocol_spec(runtime, config).request_handlers
    assert handlers is not None

    with pytest.raises(ValueError, match="workspace/configuration"):
        handlers["workspace/configuration"](params)


def test_pyrefly_probe_uses_external_configuration_without_runtime_config_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, client, _runtime_value, _config, captured = _run_fake(tmp_path, monkeypatch)

    assert cast(Any, outcome).gate_disposition == "pass"
    assert cast(Any, outcome).lifecycle.cold_readiness_seconds == pytest.approx(0.1)
    assert [method for method, _params in client.notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    assert all(
        method != "workspace/didChangeConfiguration"
        for method, _params in client.notifications
    )
    assert cast(Any, captured["spec"]).request_handlers is not None
    assert captured["manifest_roots"] == [tmp_path, tmp_path]


def test_pyrefly_probe_does_not_claim_clean_push_mode_without_exact_uri_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        foreign_push_diagnostics_uri=True,
    )

    typed = cast(Any, outcome)
    assert typed.lifecycle.diagnostics_mode == "push"
    assert typed.gate_disposition == "fail"
    assert any("push diagnostics remain unproven" in issue for issue in typed.issues)


def test_pyrefly_probe_does_not_treat_an_empty_exact_uri_publish_as_diagnostic_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        empty_push_diagnostics=True,
    )

    typed = cast(Any, outcome)
    assert typed.lifecycle.diagnostics_mode == "push"
    assert typed.gate_disposition == "fail"
    assert any("push diagnostics remain unproven" in issue for issue in typed.issues)


def test_pyrefly_probe_ignores_pre_open_and_stale_version_push_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        push_diagnostics_version=0,
    )

    typed = cast(Any, outcome)
    assert typed.lifecycle.diagnostics_mode == "push"
    assert typed.gate_disposition == "fail"
    assert any("push diagnostics remain unproven" in issue for issue in typed.issues)


def test_pyrefly_probe_raises_typed_workspace_mutation_for_any_manifest_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PyreflyWorkspaceMutation, match="changed the evaluated workspace") as raised:
        _run_fake(tmp_path, monkeypatch, manifests=("before", "after"))

    assert raised.value.before_manifest == "before"
    assert raised.value.after_manifest == "after"


def test_pyrefly_probe_requires_manifest_reserve_before_capture_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    runtime, _config, _interpreter = _runtime(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("zero reserve must be rejected before probe side effects")

    import scripts.backend_eval.pyrefly_probe as module

    monkeypatch.setattr(module, "_capture_workspace_manifest", forbidden)
    monkeypatch.setattr(module, "run_protocol_probe", forbidden)
    with pytest.raises(ValueError, match="positive.*reserve"):
        run_pyrefly_capability_probe(
            runtime,
            tmp_path,
            target,
            (0, 0),
            deadline=Deadline.start(_FakeClock(), 10.0),
        )


def test_pyrefly_after_manifest_keeps_the_same_collection_deadline_and_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config, captured = _run_fake(
        tmp_path,
        monkeypatch,
        reserve=2.0,
        after_protocol_advance=7.0,
    )

    assert cast(Any, outcome).gate_disposition == "pass"
    before_deadline, after_deadline = cast(list[Deadline], captured["manifest_deadlines"])
    assert captured["deadline"] is before_deadline
    assert after_deadline is before_deadline
    assert before_deadline.reserve == 2.0
    assert after_deadline.reserve == 2.0
    assert after_deadline.started == before_deadline.started
    assert after_deadline.seconds == before_deadline.seconds


def test_pyrefly_detects_mutation_within_the_collection_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PyreflyWorkspaceMutation) as raised:
        _run_fake(
            tmp_path,
            monkeypatch,
            manifests=("before", "after"),
            reserve=2.0,
            after_protocol_advance=7.0,
        )

    assert raised.value.before_manifest == "before"
    assert raised.value.after_manifest == "after"


def test_pyrefly_collection_exhaustion_fails_closed_with_computed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(DeadlineExceeded) as raised:
        _run_fake(
            tmp_path,
            monkeypatch,
            reserve=2.0,
            after_protocol_advance=9.5,
        )

    outcome = cast(Any, raised.value).pyrefly_capability_outcome
    assert outcome.gate_disposition == "pass"


def test_primary_protocol_error_precedes_after_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses["textDocument/definition"] = TimeoutError("semantic-timeout")

    with pytest.raises(TimeoutError, match="semantic-timeout") as raised:
        _run_fake(
            tmp_path,
            monkeypatch,
            responses=responses,
            manifests=("before", DeadlineExceeded("after-manifest-timeout")),
        )

    assert any("after-manifest-timeout" in note for note in raised.value.__notes__)


def test_workspace_mutation_remains_typed_when_protocol_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses["textDocument/definition"] = TimeoutError("semantic-timeout")

    with pytest.raises(PyreflyWorkspaceMutation) as raised:
        _run_fake(
            tmp_path,
            monkeypatch,
            responses=responses,
            manifests=("before", "after"),
        )

    assert isinstance(raised.value.__cause__, TimeoutError)


def test_pyrefly_probe_exercises_and_normalizes_all_five_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, client, _runtime_value, _config, _captured = _run_fake(tmp_path, monkeypatch)
    typed = cast(Any, outcome)

    assert [capability.name for capability in typed.capabilities] == [
        "definition",
        "document_symbols",
        "implementation",
        "references",
        "workspace_symbols",
    ]
    assert all(capability.advertised for capability in typed.capabilities)
    assert all(capability.accepted is True for capability in typed.capabilities)
    assert all(capability.normalized_valid is True for capability in typed.capabilities)
    assert [method for method, _params, _timeout in client.requests] == [
        "textDocument/definition",
        "textDocument/references",
        "textDocument/implementation",
        "textDocument/documentSymbol",
        "workspace/symbol",
    ]


@pytest.mark.parametrize(
    ("method", "capability_name", "empty_result"),
    [
        ("textDocument/definition", "definition", None),
        ("textDocument/definition", "definition", []),
        ("textDocument/references", "references", None),
        ("textDocument/references", "references", []),
        ("textDocument/documentSymbol", "document_symbols", None),
        ("textDocument/documentSymbol", "document_symbols", []),
        ("workspace/symbol", "workspace_symbols", None),
        ("workspace/symbol", "workspace_symbols", []),
    ],
)
def test_pyrefly_probe_rejects_empty_advertised_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    capability_name: str,
    empty_result: object,
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses[method] = empty_result

    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path, monkeypatch, responses=responses
    )
    capability = next(item for item in cast(Any, outcome).capabilities if item.name == capability_name)
    assert capability.accepted is True
    assert capability.normalized_valid is False
    assert cast(Any, outcome).gate_disposition == "fail"


@pytest.mark.parametrize("empty_result", [None, []])
def test_pyrefly_empty_implementation_is_recorded_but_deferred_from_phase2_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_result: object,
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses["textDocument/implementation"] = empty_result

    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        responses=responses,
    )

    implementation = next(
        capability
        for capability in cast(Any, outcome).capabilities
        if capability.name == "implementation"
    )
    assert implementation.advertised is True
    assert implementation.accepted is True
    assert implementation.normalized_valid is False
    assert implementation.task_utility == "deferred_to_feature_phase"
    assert cast(Any, outcome).gate_disposition == "pass"


@pytest.mark.parametrize(
    ("method", "capability_name", "malformed"),
    [
        ("textDocument/definition", "definition", "bad"),
        ("textDocument/references", "references", ["bad"]),
        ("textDocument/documentSymbol", "document_symbols", ["bad"]),
        ("workspace/symbol", "workspace_symbols", [{"name": "Known", "kind": 12}]),
        (
            "workspace/symbol",
            "workspace_symbols",
            [
                {
                    "name": "Known",
                    "kind": 12,
                    "location": {
                        "uri": "file:///known.py",
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                    },
                },
                "bad",
            ],
        ),
    ],
)
def test_pyrefly_probe_rejects_malformed_provider_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    capability_name: str,
    malformed: object,
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses[method] = malformed

    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path, monkeypatch, responses=responses
    )
    capability = next(item for item in cast(Any, outcome).capabilities if item.name == capability_name)
    assert capability.accepted is True
    assert capability.normalized_valid is False
    assert cast(Any, outcome).gate_disposition == "fail"


@pytest.mark.parametrize(
    ("missing", "missing_method"),
    [
        ("definition", "textDocument/definition"),
        ("references", "textDocument/references"),
        ("document_symbols", "textDocument/documentSymbol"),
        ("workspace_symbols", "workspace/symbol"),
    ],
)
def test_pyrefly_session_uses_validated_provider_advertisement_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    missing_method: str,
) -> None:
    providers = _providers(**{missing: False})
    outcome, client, _runtime_value, _config, captured = _run_fake(
        tmp_path,
        monkeypatch,
        providers=providers,
    )

    requested_methods = [method for method, _params, _timeout in client.requests]
    assert captured["session_providers"] is providers
    assert missing_method not in requested_methods
    assert len(requested_methods) == 4
    assert all(requested_methods.count(method) == 1 for method in requested_methods)
    assert cast(Any, outcome).gate_disposition == "fail"
    assert any(missing in issue and "advertise" in issue for issue in cast(Any, outcome).issues)


def test_pyrefly_pull_diagnostics_advertisement_is_seam_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        diagnostic_provider=True,
    )
    typed = cast(Any, outcome)

    assert typed.lifecycle.diagnostics_mode == "pull"
    assert typed.gate_disposition == "seam_incompatible_pull_only"
    assert any("pull" in issue and "push" in issue for issue in typed.issues)


def test_pyrefly_probe_preserves_typed_lsp_errors_and_fresh_request_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses["textDocument/references"] = LspResponseError(-32801, "content modified")
    responses["textDocument/implementation"] = LspResponseError(-32800, "request cancelled")

    outcome, client, _runtime_value, _config, _captured = _run_fake(
        tmp_path, monkeypatch, responses=responses
    )
    typed = cast(Any, outcome)
    references = next(item for item in typed.capabilities if item.name == "references")
    implementation = next(item for item in typed.capabilities if item.name == "implementation")
    assert references.accepted is False
    assert implementation.accepted is False
    assert "-32801" in references.notes
    assert "-32800" in implementation.notes
    assert typed.lifecycle.content_modified_count == 1
    assert typed.lifecycle.request_cancelled_count == 1
    assert typed.gate_disposition == "fail"
    timeouts = [timeout for _method, _params, timeout in client.requests]
    assert all(timeout is not None and timeout > 0 for timeout in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)
    assert client.notifications[-1][0] == "textDocument/didClose"


def test_pyrefly_redacts_and_bounds_server_error_notes_and_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    secret = "malicious-pyrefly-secret"
    responses = _responses(target)
    responses["textDocument/definition"] = LspResponseError(
        -32001,
        f"password={secret} " + "x" * 5000,
    )

    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        responses=responses,
    )
    typed = cast(Any, outcome)
    definition = next(item for item in typed.capabilities if item.name == "definition")
    assert secret not in definition.notes
    assert "<redacted>" in definition.notes
    assert len(definition.notes) <= 1024
    assert all(secret not in issue and len(issue) <= 1024 for issue in typed.issues)


def test_primary_semantic_error_precedes_did_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    responses = _responses(target)
    responses["textDocument/definition"] = TimeoutError("semantic-timeout")
    clock = _FakeClock()
    client = _FakeClient(clock, responses, close_error=RuntimeError("didClose-failure"))
    runtime, _config, _interpreter = _runtime(tmp_path)
    _install_fake_runner(monkeypatch, client, {})

    with pytest.raises(TimeoutError, match="semantic-timeout") as raised:
        run_pyrefly_capability_probe(
            runtime,
            tmp_path,
            target,
            (0, 0),
            deadline=Deadline.start(clock, 10.0, reserve=1.0),
        )

    assert any("didClose-failure" in note for note in raised.value.__notes__)


def test_did_close_failure_alone_fails_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config, _captured = _run_fake(
        tmp_path,
        monkeypatch,
        close_error=RuntimeError("didClose-failure"),
    )

    assert cast(Any, outcome).gate_disposition == "fail"
    assert any("didClose-failure" in issue for issue in cast(Any, outcome).issues)


@pytest.mark.parametrize("position", [(-1, 0), (0, -1), (True, 0), (0, False)])
def test_pyrefly_probe_rejects_invalid_symbol_positions_before_manifest_or_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: tuple[int, int],
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    runtime, _config, _interpreter = _runtime(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid position must not capture or start the protocol runner")

    import scripts.backend_eval.pyrefly_probe as module

    monkeypatch.setattr(module, "_capture_workspace_manifest", forbidden)
    monkeypatch.setattr(module, "run_protocol_probe", forbidden)
    with pytest.raises(ValueError, match="non-negative integers"):
        run_pyrefly_capability_probe(
            runtime,
            tmp_path,
            target,
            position,
            deadline=Deadline.start(_FakeClock(), 10.0),
        )
