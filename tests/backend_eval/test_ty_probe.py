"""Locked-ty protocol-plane facts and deterministic capability behavior."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.backend_eval.models import (
    CandidateProtocolOutcome,
    EnvironmentIdentity,
    ServiceConfigIdentity,
)
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.protocol import ProtocolSession
from scripts.backend_eval.runtime import CandidateRuntime, minimal_backend_environment
from scripts.backend_eval.ty_probe import run_ty_capability_probe, ty_protocol_spec
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

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        self.requests.append((method, params, timeout))
        self.clock.advance(0.1)
        response = self.responses[method]
        if isinstance(response, BaseException):
            raise response
        return response

    def notify(self, method: str, params: object = None) -> None:
        self.notifications.append((method, params))
        if method == "textDocument/didClose" and self.close_error is not None:
            raise self.close_error


def _runtime(tmp_path: Path) -> tuple[CandidateRuntime, ServiceConfigIdentity]:
    root = tmp_path / _LOCK_DIGEST
    config_root = root / "config"
    ty_config = ServiceConfigIdentity(
        backend="ty",
        config_path=str(config_root / "ty/ty.toml"),
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
                interpreter_path=str(root / "environments/ms/bin/python"),
                interpreter_realpath=str(root / "environments/ms/bin/python"),
                version="3.12.11",
            ),
        ),
        service_configs=(
            ServiceConfigIdentity(
                backend="pyrefly",
                config_path=str(config_root / "pyrefly/pyrefly.toml"),
                config_sha256="f" * 64,
                home_path=str(root / "home"),
                cache_path=str(root / "cache"),
            ),
            ServiceConfigIdentity(
                backend="pyright",
                config_path=str(config_root / "pyright/pyrightconfig.json"),
                config_sha256="1" * 64,
                home_path=str(root / "home"),
                cache_path=str(root / "cache"),
            ),
            ty_config,
        ),
        manifest_path=root / "runtime-manifest.json",
        manifest_sha256=_MANIFEST_DIGEST,
    )
    return runtime, ty_config


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
        "textDocument/documentSymbol": [
            {
                "name": "Known",
                "kind": 12,
                "range": location["range"],
                "selectionRange": location["range"],
            }
        ],
        "workspace/symbol": [
            {"name": "Known", "kind": 12, "location": location}
        ],
    }


def _providers(**overrides: bool) -> RawLspProviders:
    fields = {
        "definition": True,
        "implementation": False,
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
    exit_status: int | None = 0,
) -> None:
    import scripts.backend_eval.ty_probe as module

    def fake_run_protocol_probe(
        spec: object,
        runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Any,
    ) -> ProtocolSession[object]:
        captured.update(
            spec=spec,
            runtime=runtime,
            workspace_root=workspace_root,
            deadline=deadline,
        )
        typed_spec = cast(Any, spec)
        active_providers = providers or _providers()
        return ProtocolSession(
            raw_providers=active_providers,
            diagnostic_provider=True,
            position_encoding=typed_spec.position_encoding,
            engine=typed_spec.engine(runtime),
            stderr_tail="",
            terminal_errors=(),
            cleanup_errors=(),
            exit_status=exit_status,
            result=session(client, active_providers),
        )

    monkeypatch.setattr(module, "run_protocol_probe", fake_run_protocol_probe)


def _run_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: Mapping[str, object] | None = None,
    providers: RawLspProviders | None = None,
    close_error: BaseException | None = None,
) -> tuple[CandidateProtocolOutcome, _FakeClient, CandidateRuntime, ServiceConfigIdentity]:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    clock = _FakeClock()
    client = _FakeClient(clock, responses or _responses(target), close_error=close_error)
    runtime, config = _runtime(tmp_path)
    _install_fake_runner(monkeypatch, client, {}, providers=providers)
    outcome = run_ty_capability_probe(
        runtime,
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(clock, 10.0),
    )
    return outcome, client, runtime, config


def test_ty_protocol_spec_binds_locked_command_config_and_interpreter(tmp_path: Path) -> None:
    runtime, config = _runtime(tmp_path)
    selected_interpreter = Path(runtime.environments[0].interpreter_path)

    spec = ty_protocol_spec(runtime, config)

    assert spec.build_command(runtime) == (str(runtime.ty), "server")
    assert spec.request_handlers is not None
    assert spec.position_encoding is PositionEncoding.UTF16
    assert spec.diagnostics_mode == "pull"
    assert spec.engine(runtime).name == "ty"
    assert spec.engine(runtime).version == "0.0.70"
    assert spec.engine(runtime).executable == runtime.ty
    assert spec.engine(runtime).interpreter == selected_interpreter
    with pytest.raises(ValueError, match="exact caller-bound runtime"):
        spec.build_command(cast(CandidateRuntime, object()))
    with pytest.raises(ValueError, match="exact caller-bound runtime"):
        spec.engine(cast(CandidateRuntime, object()))
    params = spec.initialize_params(tmp_path)
    assert params["rootPath"] == str(tmp_path)
    assert params["rootUri"] == tmp_path.as_uri()
    assert params["initializationOptions"] == {}
    assert cast(Any, params)["capabilities"]["workspace"]["configuration"] is True
    assert spec.request_handlers["workspace/configuration"](
        {"items": [{"scopeUri": tmp_path.as_uri(), "section": "ty"}]}
    ) == [
        {
            "configurationFile": config.config_path,
            "configuration": {"environment": {"python": str(selected_interpreter)}},
        }
    ]
    environment = minimal_backend_environment(runtime, selected_interpreter)
    assert environment["SERENA_LIGHT_SELECTED_PYTHON"] == str(selected_interpreter)
    assert environment["XDG_CONFIG_HOME"] == str(runtime.config)
    assert not any(key.upper().endswith("_PROXY") for key in environment)


def test_ty_protocol_spec_rejects_a_config_not_bound_to_runtime(tmp_path: Path) -> None:
    runtime, config = _runtime(tmp_path)
    foreign = ServiceConfigIdentity(
        backend="ty",
        config_path=str(tmp_path / "foreign/ty.toml"),
        config_sha256=config.config_sha256,
        home_path=config.home_path,
        cache_path=config.cache_path,
    )

    with pytest.raises(ValueError, match="service-owned ty configuration"):
        ty_protocol_spec(runtime, foreign)


@pytest.mark.parametrize(
    "params",
    [
        None,
        {"items": None},
        {"items": "bad"},
        {"items": []},
        {"items": ["bad"]},
        {"items": [{"scopeUri": "/foreign", "section": "ty"}]},
        {"items": [{"scopeUri": "file:///foreign", "section": "python"}]},
        {
            "items": [
                {"scopeUri": "file:///workspace", "section": "ty"},
                {"scopeUri": "file:///workspace", "section": "ty"},
            ]
        },
    ],
)
def test_ty_workspace_configuration_rejects_malformed_or_foreign_requests(
    tmp_path: Path, params: object
) -> None:
    runtime, config = _runtime(tmp_path)
    spec = ty_protocol_spec(runtime, config)
    assert spec.request_handlers is not None
    spec.initialize_params(tmp_path)

    with pytest.raises(ValueError, match="ty workspace/configuration"):
        spec.request_handlers["workspace/configuration"](params)


def test_ty_workspace_configuration_refuses_before_initialize_params_bind_scope(
    tmp_path: Path,
) -> None:
    runtime, config = _runtime(tmp_path)
    handlers = ty_protocol_spec(runtime, config).request_handlers
    assert handlers is not None

    with pytest.raises(ValueError, match="before initialize params"):
        handlers["workspace/configuration"](
            {"items": [{"scopeUri": tmp_path.as_uri(), "section": "ty"}]}
        )


def test_ty_probe_uses_server_consumed_configuration_not_an_unproven_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, client, runtime, config = _run_fake(tmp_path, monkeypatch)
    assert outcome.gate_disposition == "pass"
    assert outcome.lifecycle.cold_readiness_seconds == pytest.approx(0.1)
    assert [method for method, _params in client.notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    spec = ty_protocol_spec(runtime, config)
    params = spec.initialize_params(tmp_path)
    assert spec.request_handlers is not None
    assert spec.request_handlers["workspace/configuration"](
        {"items": [{"scopeUri": tmp_path.as_uri(), "section": "ty"}]}
    )[0]["configurationFile"] == config.config_path
    assert cast(Any, params)["capabilities"]["workspace"]["configuration"] is True


def test_ty_probe_records_explicit_negative_implementation_without_requesting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, client, _runtime_value, _config = _run_fake(tmp_path, monkeypatch)

    assert [capability.name for capability in outcome.capabilities] == [
        "definition",
        "document_symbols",
        "implementation",
        "references",
        "workspace_symbols",
    ]
    implementation = next(item for item in outcome.capabilities if item.name == "implementation")
    assert implementation.advertised is False
    assert implementation.accepted is None
    assert implementation.normalized_valid is None
    assert implementation.notes == "locked ty version does not advertise textDocument/implementation"
    assert "textDocument/implementation" not in [method for method, _params, _timeout in client.requests]


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
def test_ty_probe_rejects_empty_advertised_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    capability_name: str,
    empty_result: object,
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses[method] = empty_result

    outcome, _client, _runtime_value, _config = _run_fake(
        tmp_path, monkeypatch, responses=responses
    )

    capability = next(item for item in outcome.capabilities if item.name == capability_name)
    assert capability.accepted is True
    assert capability.normalized_valid is False
    assert outcome.gate_disposition == "fail"


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
def test_ty_probe_rejects_malformed_provider_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    capability_name: str,
    malformed: object,
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses[method] = malformed

    outcome, _client, _runtime_value, _config = _run_fake(
        tmp_path, monkeypatch, responses=responses
    )

    capability = next(item for item in outcome.capabilities if item.name == capability_name)
    assert capability.accepted is True
    assert capability.normalized_valid is False
    assert outcome.gate_disposition == "fail"


@pytest.mark.parametrize(
    "missing",
    ["definition", "references", "document_symbols", "workspace_symbols"],
)
def test_ty_gate_requires_every_baseline_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    outcome, _client, _runtime_value, _config = _run_fake(
        tmp_path,
        monkeypatch,
        providers=_providers(**{missing: False}),
    )

    assert outcome.gate_disposition == "fail"
    assert any(missing in issue and "advertise" in issue for issue in outcome.issues)


def test_advertised_implementation_is_requested_once_and_records_normalized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = _responses(tmp_path / "known.py")
    responses["textDocument/implementation"] = responses["textDocument/references"]
    outcome, client, _runtime_value, _config = _run_fake(
        tmp_path,
        monkeypatch,
        responses=responses,
        providers=_providers(implementation=True),
    )

    implementation = next(item for item in outcome.capabilities if item.name == "implementation")
    assert implementation.advertised is True
    assert implementation.accepted is True
    assert implementation.normalized_valid is True
    assert implementation.notes == ""
    assert outcome.gate_disposition == "pass"
    assert [method for method, _params, _timeout in client.requests].count(
        "textDocument/implementation"
    ) == 1


def test_ty_probe_preserves_typed_lsp_error_and_fresh_request_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses["textDocument/references"] = LspResponseError(-32801, "content modified")

    outcome, client, _runtime_value, _config = _run_fake(
        tmp_path, monkeypatch, responses=responses
    )

    references = next(item for item in outcome.capabilities if item.name == "references")
    assert references.accepted is False
    assert references.normalized_valid is False
    assert "-32801" in references.notes
    assert outcome.lifecycle.content_modified_count == 1
    assert outcome.gate_disposition == "fail"
    timeouts = [timeout for _method, _params, timeout in client.requests]
    assert all(timeout is not None and timeout > 0 for timeout in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)
    assert client.notifications[-1][0] == "textDocument/didClose"


def test_ty_redacts_and_bounds_server_error_notes_and_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    secret = "malicious-ty-secret"
    responses = _responses(target)
    responses["textDocument/definition"] = LspResponseError(
        -32001,
        f"password={secret} " + "x" * 5000,
    )

    outcome, _client, _runtime_value, _config = _run_fake(
        tmp_path,
        monkeypatch,
        responses=responses,
    )

    definition = next(item for item in outcome.capabilities if item.name == "definition")
    assert secret not in definition.notes
    assert "<redacted>" in definition.notes
    assert len(definition.notes) <= 1024
    assert all(secret not in issue and len(issue) <= 1024 for issue in outcome.issues)


def test_primary_semantic_error_precedes_did_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    responses = _responses(target)
    responses["textDocument/definition"] = TimeoutError("semantic-timeout")
    target.write_text("Known = 1\n", encoding="utf-8")
    clock = _FakeClock()
    client = _FakeClient(
        clock,
        responses,
        close_error=RuntimeError("didClose-failure"),
    )
    runtime, _config = _runtime(tmp_path)
    _install_fake_runner(monkeypatch, client, {})

    with pytest.raises(TimeoutError, match="semantic-timeout") as raised:
        run_ty_capability_probe(
            runtime,
            tmp_path,
            target,
            (0, 0),
            deadline=Deadline.start(clock, 10.0),
        )

    assert any("didClose-failure" in note for note in raised.value.__notes__)


def test_did_close_failure_alone_fails_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _client, _runtime_value, _config = _run_fake(
        tmp_path,
        monkeypatch,
        close_error=RuntimeError("didClose-failure"),
    )

    assert outcome.gate_disposition == "fail"
    assert any("didClose-failure" in issue for issue in outcome.issues)


@pytest.mark.parametrize("position", [(-1, 0), (0, -1), (True, 0), (0, False)])
def test_ty_probe_rejects_invalid_symbol_positions_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: tuple[int, int],
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    runtime, _config = _runtime(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid position must not start the protocol runner")

    import scripts.backend_eval.ty_probe as module

    monkeypatch.setattr(module, "run_protocol_probe", forbidden)
    with pytest.raises(ValueError, match="non-negative integers"):
        run_ty_capability_probe(
            runtime,
            tmp_path,
            target,
            position,
            deadline=Deadline.start(_FakeClock(), 10.0),
        )
