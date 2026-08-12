"""Pyright protocol-plane facts, capability, and one real read-only slice."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.backend_eval.manifests import RootManifestRequest, capture_root_manifest, default_corpus_requests
from scripts.backend_eval.models import CandidateProtocolOutcome, RootManifest
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.protocol import ProtocolSession
from scripts.backend_eval.pyright_probe import pyright_protocol_spec, run_pyright_capability_probe
from scripts.backend_eval.runtime import CandidateRuntime
from serena_light.lsp.adapter import EngineMetadata, RawLspProviders
from serena_light.lsp.client import LspResponseError
from serena_light.lsp.positions import PositionEncoding
from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import MS_INTERPRETER
from tests.backend_eval.support import real_expectation

MS_SWIFT = Path("/data/ms-swift")
MS_SITE_PACKAGES = MS_INTERPRETER.parents[1] / "lib" / "python3.12" / "site-packages"
TRANSFORMERS_ROOT = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)
KNOWN_FILE = MS_SWIFT / "swift/infer_engine/lmdeploy_engine.py"
KNOWN_POSITION = (14, 25)

pytestmark = pytest.mark.timeout(90)


@dataclass
class _FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _facts(interpreter: Path) -> PyrightFacts:
    return PyrightFacts(
        extensions=frozenset({".py", ".pyi"}),
        language_id="python",
        command=("/runtime/node", "/runtime/pyright-langserver", "--stdio"),
        engine_path=Path("/runtime/pyright"),
        engine_version="1.1.403",
        interpreter=interpreter,
    )


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


def _responses(target: Path) -> dict[str, object]:
    uri = target.as_uri()
    location = {
        "uri": uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 4},
        },
    }
    symbol = {
        "name": "Known",
        "kind": 12,
        "range": location["range"],
        "selectionRange": location["range"],
    }
    workspace_symbol = {"name": "Known", "kind": 12, "location": location}
    return {
        "textDocument/definition": location,
        "textDocument/references": [location],
        "textDocument/implementation": [location],
        "textDocument/documentSymbol": [symbol],
        "workspace/symbol": [workspace_symbol],
    }


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    captured: dict[str, object],
    *,
    providers: RawLspProviders | None = None,
) -> None:
    import scripts.backend_eval.pyright_probe as module

    runtime = cast(CandidateRuntime, object())
    monkeypatch.setattr(module, "_prepared_candidate_runtime", lambda: runtime)

    def fake_run_protocol_probe(
        spec: object,
        candidate_runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Any,
    ) -> ProtocolSession[object]:
        captured.update(
            spec=spec,
            runtime=candidate_runtime,
            workspace_root=workspace_root,
            deadline=deadline,
        )
        result = session(client)
        return ProtocolSession(
            raw_providers=providers
            or RawLspProviders(
                definition=True,
                implementation=True,
                references=True,
                document_symbols=True,
                workspace_symbols=True,
            ),
            diagnostic_provider=False,
            position_encoding=PositionEncoding.UTF16,
            engine=EngineMetadata(
                name="pyright",
                version="1.1.403",
                executable=Path("/runtime/pyright-langserver"),
                interpreter=Path("/runtime/python"),
            ),
            stderr_tail="",
            terminal_errors=(),
            cleanup_errors=(),
            exit_status=0,
            result=result,
        )

    monkeypatch.setattr(module, "run_protocol_probe", fake_run_protocol_probe)


def test_pyright_protocol_spec_delegates_all_locked_facts(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"")
    facts = _facts(interpreter)

    spec = pyright_protocol_spec(facts)

    assert spec.build_command(cast(CandidateRuntime, object())) is facts.command
    assert spec.initialize_params(tmp_path) == facts.initialize_params(tmp_path)
    assert spec.request_handlers is not None
    assert spec.request_handlers["workspace/configuration"](
        {"items": [{"section": "python"}, {"section": "python.analysis"}]}
    ) == facts.workspace_configuration(
        {"items": [{"section": "python"}, {"section": "python.analysis"}]}
    )
    assert spec.engine(cast(CandidateRuntime, object())) == facts.adapter_language_facts(tmp_path).engine
    assert spec.position_encoding is PositionEncoding.UTF16
    assert spec.diagnostics_mode == "push"


def test_pyright_capability_probe_exercises_and_normalizes_every_advertised_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    clock = _FakeClock()
    client = _FakeClient(clock, _responses(target))
    captured: dict[str, object] = {}
    _install_fake_runner(monkeypatch, client, captured)

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(clock, 10.0),
    )

    assert captured["workspace_root"] == tmp_path
    assert [capability.name for capability in outcome.capabilities] == [
        "definition",
        "document_symbols",
        "implementation",
        "references",
        "workspace_symbols",
    ]
    assert all(capability.advertised for capability in outcome.capabilities)
    assert all(capability.accepted is True for capability in outcome.capabilities)
    assert all(capability.normalized_valid is True for capability in outcome.capabilities)
    assert outcome.gate_disposition == "pass"
    assert [method for method, _params, _timeout in client.requests] == [
        "textDocument/definition",
        "textDocument/references",
        "textDocument/implementation",
        "textDocument/documentSymbol",
        "workspace/symbol",
    ]
    timeouts = [timeout for _method, _params, timeout in client.requests]
    assert all(timeout is not None and timeout > 0 for timeout in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)
    assert [method for method, _params in client.notifications] == [
        "workspace/didChangeConfiguration",
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    assert client.notifications[0][1] == {"settings": {}}


def test_pyright_capability_probe_records_typed_lsp_error_and_still_closes_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    clock = _FakeClock()
    responses = _responses(target)
    responses["textDocument/implementation"] = LspResponseError(-32801, "content modified")
    client = _FakeClient(clock, responses)
    _install_fake_runner(monkeypatch, client, {})

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(clock, 10.0),
    )

    implementation = next(capability for capability in outcome.capabilities if capability.name == "implementation")
    assert implementation.accepted is False
    assert implementation.normalized_valid is False
    assert "-32801" in implementation.notes
    assert outcome.lifecycle.content_modified_count == 1
    assert outcome.gate_disposition == "fail"
    assert client.notifications[-1][0] == "textDocument/didClose"


@pytest.mark.parametrize(
    ("method", "capability_name", "empty_result"),
    [
        ("textDocument/definition", "definition", None),
        ("textDocument/definition", "definition", []),
        ("textDocument/references", "references", None),
        ("textDocument/references", "references", []),
        ("textDocument/implementation", "implementation", None),
        ("textDocument/implementation", "implementation", []),
        ("textDocument/documentSymbol", "document_symbols", None),
        ("textDocument/documentSymbol", "document_symbols", []),
        ("workspace/symbol", "workspace_symbols", None),
        ("workspace/symbol", "workspace_symbols", []),
    ],
)
def test_advertised_empty_results_are_accepted_but_fail_normalized_validity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    capability_name: str,
    empty_result: object,
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    responses = _responses(target)
    responses[method] = empty_result
    client = _FakeClient(_FakeClock(), responses)
    _install_fake_runner(monkeypatch, client, {})

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(client.clock, 10.0),
    )

    capability = next(item for item in outcome.capabilities if item.name == capability_name)
    assert capability.advertised is True
    assert capability.accepted is True
    assert capability.normalized_valid is False
    assert outcome.gate_disposition == "fail"


@pytest.mark.parametrize(
    "missing",
    ["definition", "references", "document_symbols", "workspace_symbols"],
)
def test_pyright_gate_requires_current_baseline_advertisements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    advertised = {
        "definition": True,
        "implementation": False,
        "references": True,
        "document_symbols": True,
        "workspace_symbols": True,
    }
    advertised[missing] = False
    providers = RawLspProviders(**advertised)
    client = _FakeClient(_FakeClock(), _responses(target))
    _install_fake_runner(monkeypatch, client, {}, providers=providers)

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(client.clock, 10.0),
    )

    assert outcome.gate_disposition == "fail"
    assert any(missing in issue and "advertise" in issue for issue in outcome.issues)


def test_unadvertised_implementation_remains_explicitly_unsupported_without_failing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    responses = _responses(target)
    responses["textDocument/implementation"] = None
    providers = RawLspProviders(
        definition=True,
        implementation=False,
        references=True,
        document_symbols=True,
        workspace_symbols=True,
    )
    client = _FakeClient(_FakeClock(), responses)
    _install_fake_runner(monkeypatch, client, {}, providers=providers)

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(client.clock, 10.0),
    )

    implementation = next(item for item in outcome.capabilities if item.name == "implementation")
    assert implementation.advertised is False
    assert implementation.normalized_valid is False
    assert "not advertised" in implementation.notes
    assert outcome.gate_disposition == "pass"


@pytest.mark.parametrize(
    "workspace_symbols",
    [
        [
            {
                "kind": 12,
                "location": {
                    "uri": "file:///known.py",
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                },
            }
        ],
        [
            {
                "name": "Known",
                "location": {
                    "uri": "file:///known.py",
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                },
            }
        ],
        [{"name": "Known", "kind": 12, "location": {"uri": "file:///known.py"}}],
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
    ],
    ids=["missing-name", "missing-kind", "bad-location", "mixed-list"],
)
def test_workspace_symbols_require_the_full_production_symbol_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_symbols: object
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    responses = _responses(target)
    responses["workspace/symbol"] = workspace_symbols
    client = _FakeClient(_FakeClock(), responses)
    _install_fake_runner(monkeypatch, client, {})

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(client.clock, 10.0),
    )

    capability = next(item for item in outcome.capabilities if item.name == "workspace_symbols")
    assert capability.accepted is True
    assert capability.normalized_valid is False
    assert outcome.gate_disposition == "fail"


def test_semantic_timeout_remains_primary_when_did_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    responses = _responses(target)
    responses["textDocument/definition"] = TimeoutError("semantic-timeout")
    client = _FakeClient(_FakeClock(), responses, close_error=RuntimeError("didClose-failure"))
    _install_fake_runner(monkeypatch, client, {})

    with pytest.raises(TimeoutError, match="semantic-timeout") as raised:
        run_pyright_capability_probe(
            _facts(tmp_path / "python"),
            tmp_path,
            target,
            (0, 0),
            deadline=Deadline.start(client.clock, 10.0),
        )

    assert any("didClose-failure" in note for note in raised.value.__notes__)


def test_normalization_failure_remains_visible_when_did_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    responses = _responses(target)
    responses["workspace/symbol"] = [{"kind": 12, "location": _responses(target)["textDocument/definition"]}]
    client = _FakeClient(_FakeClock(), responses, close_error=RuntimeError("didClose-failure"))
    _install_fake_runner(monkeypatch, client, {})

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(client.clock, 10.0),
    )

    assert outcome.gate_disposition == "fail"
    assert any("workspace_symbols" in issue for issue in outcome.issues)
    assert any("didClose-failure" in issue for issue in outcome.issues)


def test_did_close_failure_alone_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "known.py"
    target.write_text("Known = 1\n", encoding="utf-8")
    client = _FakeClient(_FakeClock(), _responses(target), close_error=RuntimeError("didClose-failure"))
    _install_fake_runner(monkeypatch, client, {})

    outcome = run_pyright_capability_probe(
        _facts(tmp_path / "python"),
        tmp_path,
        target,
        (0, 0),
        deadline=Deadline.start(client.clock, 10.0),
    )

    assert outcome.gate_disposition == "fail"
    assert any("didClose-failure" in issue for issue in outcome.issues)


@pytest.fixture(scope="module")
def ms_swift_request() -> RootManifestRequest:
    return next(request for request in default_corpus_requests() if request.root == MS_SWIFT)


@pytest.fixture(scope="module")
def real_pyright_slice(
    ms_swift_request: RootManifestRequest,
) -> tuple[CandidateProtocolOutcome, RootManifest, RootManifest]:
    before = capture_root_manifest(ms_swift_request, expectation=real_expectation())
    outcome = run_pyright_capability_probe(
        PyrightFacts.locked(root=Path("/data/CoordExp/serena-light"), interpreter=MS_INTERPRETER),
        MS_SWIFT,
        KNOWN_FILE,
        KNOWN_POSITION,
        deadline=Deadline.start(monotonic_clock, 90.0),
    )
    after = capture_root_manifest(ms_swift_request, expectation=real_expectation())
    return outcome, before, after


@pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")
@pytest.mark.external_repo(root=str(TRANSFORMERS_ROOT), snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT")
def test_real_pyright_capability_probe_reports_normalized_definition(
    real_pyright_slice: tuple[CandidateProtocolOutcome, RootManifest, RootManifest],
) -> None:
    outcome = real_pyright_slice[0]
    definition = next(capability for capability in outcome.capabilities if capability.name == "definition")
    assert definition.advertised is True
    assert definition.accepted is True
    assert definition.normalized_valid is True
    assert outcome.gate_disposition == "pass"
    assert outcome.lifecycle.shutdown_clean is True
    assert outcome.lifecycle.cleanup_clean is True


@pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")
@pytest.mark.external_repo(root=str(TRANSFORMERS_ROOT), snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT")
def test_real_pyright_probe_leaves_no_write(
    real_pyright_slice: tuple[CandidateProtocolOutcome, RootManifest, RootManifest],
) -> None:
    _outcome, before, after = real_pyright_slice
    assert after == before
