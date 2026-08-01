from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import pytest

from serena_light.lsp.adapter import DocumentSymbolReadinessProbe, PublishedDiagnosticsWitness
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.normalize import normalize_document_symbols
from serena_light.lsp.pyright import PyrightFacts
from serena_light.lsp.state import DiagnosticsState, LspState
from serena_light.lsp.typescript import (
    TypeScriptAdapterConfig,
    attribute_native_program,
    probe_inferred_path_support,
)
from serena_light.processes import LanguageServerSubprocessLauncher, terminate_process_tree_with_kill_fallback
from serena_light.workspace.inventory import git_trust_inventory
from serena_light.workspace.runtime import _WorkspaceLanguageAdapter
from serena_light.workspace.scope import (
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeGenerationTracker,
    ScopeProjection,
)

ROOT = Path("/data/CoordExp/external/codexUI")
MS_SWIFT = Path("/data/ms-swift")
# ``tsconfig.json`` selects ``src/**/*.ts`` plus ``vite.config.ts``.  These two
# are inside that configured program and import across the file boundary; the
# ``.mjs`` script is Git-trusted but outside it.
DECLARATION_OWNER = "src/api/codexErrors.ts"
DECLARATION_CONSUMER = "src/api/codexRpcClient.ts"
OMITTED_MJS = "scripts/generate-pwa-icons.mjs"

pytestmark = [
    pytest.mark.timeout(90),
    pytest.mark.external_repo(
        root=str(ROOT),
        snapshot_env="SERENA_LIGHT_CODEXUI_SNAPSHOT",
    ),
]


def _path_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _uri_path(location: Mapping[str, Any]) -> Path:
    parsed = urlparse(str(location["uri"]))
    assert parsed.scheme == "file"
    return Path(unquote(parsed.path))


def _position_of(source: str, needle: str, *, after: str = "", inside: int = 0) -> dict[str, int]:
    start = source.index(after) + len(after) if after else 0
    offset = source.index(needle, start) + inside
    prefix = source[:offset]
    return {"line": prefix.count("\n"), "character": len(prefix.rsplit("\n", 1)[-1])}


def _wait_for_matching_publication(
    publications: queue.Queue[Mapping[str, Any]],
    *,
    uri: str,
    require_findings: bool,
    timeout: float,
) -> Mapping[str, Any]:
    deadline = time.monotonic() + timeout
    collected: list[Mapping[str, Any]] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"no matching diagnostics publication arrived: {collected!r}")
        try:
            publication = publications.get(timeout=remaining)
        except queue.Empty as error:
            raise AssertionError(f"no matching diagnostics publication arrived: {collected!r}") from error
        collected.append(publication)
        diagnostics = publication.get("diagnostics")
        if publication.get("uri") == uri and isinstance(diagnostics, list) and (diagnostics or not require_findings):
            return publication


@pytest.fixture(scope="module")
def config() -> TypeScriptAdapterConfig:
    return TypeScriptAdapterConfig.locked()


@pytest.fixture(scope="module")
def real_lsp(config: TypeScriptAdapterConfig) -> Iterator[tuple[SyncLspClient, Mapping[str, Any]]]:
    process = LanguageServerSubprocessLauncher.get_instance().launch(
        config.command,
        cwd=ROOT,
        env={"PATH": str(config.node_path.parent)},
    )
    assert process.stdout is not None
    assert process.stdin is not None
    client = SyncLspClient(
        process.stdout,
        process.stdin,
        request_timeout=20.0,
        request_handlers={
            "client/registerCapability": lambda _params: None,
            "window/workDoneProgress/create": lambda _params: None,
            "workspace/configuration": lambda params: [{} for _item in (params or {}).get("items", [])],
            "workspace/executeClientCommand": lambda _params: [],
            "workspace/applyEdit": lambda _params: {
                "applied": False,
                "failureReason": "real adapter test is read-only",
            },
        },
    )
    try:
        client.start()
        initialize_result = client.request("initialize", config.initialize_params(ROOT), timeout=15.0)
        assert isinstance(initialize_result, Mapping)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        for relative in (DECLARATION_CONSUMER, DECLARATION_OWNER, OMITTED_MJS):
            path = ROOT / relative
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": _path_uri(path),
                        "languageId": config.language_id(path),
                        "version": 1,
                        "text": path.read_text(encoding="utf-8"),
                    }
                },
            )
        yield client, initialize_result
    finally:
        client.shutdown(timeout=3.0)
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree_with_kill_fallback(
                process,
                2.0,
                "real TypeScript adapter test",
                kill_timeout=2.0,
            )


def test_real_mjs_overview_and_capability_facts(
    config: TypeScriptAdapterConfig,
    real_lsp: tuple[SyncLspClient, Mapping[str, Any]],
) -> None:
    """A ``.mjs`` path outside the configured program still yields symbols."""

    client, initialize_result = real_lsp
    script = ROOT / OMITTED_MJS
    raw_symbols = client.request(
        "textDocument/documentSymbol",
        {"textDocument": {"uri": _path_uri(script)}},
        timeout=15.0,
    )
    assert isinstance(raw_symbols, list)
    symbols = normalize_document_symbols(raw_symbols, document_uri=_path_uri(script))
    names = {symbol.name for root in symbols for symbol in root.iter_depth_first()}

    assert config.language_id(script) == "javascript"
    assert {"rootDir", "iconsDir", "jobs", "browser"} <= names
    facts = config.capability_facts(initialize_result)
    assert facts.raw_providers["definitionProvider"] is True
    assert facts.raw_providers["declarationProvider"] is False
    assert facts.raw_providers["implementationProvider"] is True
    assert facts.derived_tools["find_declaration"] is True
    assert facts.derived_tools["find_implementations"] is True
    assert facts.position_encoding == "utf-16"


def test_real_cross_file_definition_references_and_implementation(
    real_lsp: tuple[SyncLspClient, Mapping[str, Any]],
) -> None:
    client, _initialize_result = real_lsp
    consumer = ROOT / DECLARATION_CONSUMER
    source = consumer.read_text(encoding="utf-8")
    position = _position_of(source, "extractErrorMessage(payload", after="const response", inside=2)
    text_document = {"uri": _path_uri(consumer)}

    definitions = client.request(
        "textDocument/definition",
        {"textDocument": text_document, "position": position},
        timeout=15.0,
    )
    references = client.request(
        "textDocument/references",
        {"textDocument": text_document, "position": position, "context": {"includeDeclaration": True}},
        timeout=15.0,
    )
    implementations = client.request(
        "textDocument/implementation",
        {"textDocument": text_document, "position": position},
        timeout=15.0,
    )

    assert isinstance(definitions, list) and len(definitions) == 1
    assert _uri_path(definitions[0]) == ROOT / DECLARATION_OWNER
    assert isinstance(references, list)
    assert {_uri_path(location) for location in references} == {
        ROOT / DECLARATION_CONSUMER,
        ROOT / DECLARATION_OWNER,
    }
    assert len(references) >= 3
    assert isinstance(implementations, list) and len(implementations) == 1
    assert _uri_path(implementations[0]) == ROOT / DECLARATION_OWNER


def test_real_status_scope_facts_and_omitted_engine_owned_inferred_path(
    config: TypeScriptAdapterConfig,
) -> None:
    inventory = git_trust_inventory(ROOT)
    attributed = attribute_native_program(
        config,
        ROOT,
        trust_inventory_paths=inventory.paths,
        entry_path=DECLARATION_OWNER,
    )
    projection = attributed.require_compatible()
    status = attributed.status_facts()
    fixed = config.fixed_facts()

    expected_trust = tuple(path for path in inventory.paths if Path(path).suffix.lower() in config.extensions)
    assert fixed["language_server_version"] == "5.1.3"
    assert fixed["typescript_engine_version"] == "5.9.3"
    assert Path(str(fixed["typescript_engine_path"])) == config.tsserver_path
    assert status["selected_config_path"] == "tsconfig.json"
    assert status["project_kind"] == "configured"
    assert status["scope_compatible"] is True
    assert status["overlay_generated"] is False
    assert projection.trust_inventory.paths == expected_trust
    assert status["trust_inventory_count"] == len(expected_trust)
    assert status["configured_program_count"] == len(projection.configured_program.paths) > 0
    assert projection.configured_program.paths
    assert not projection.configured_program_outside_trust
    assert {item.path for item in projection.trusted_not_in_configured_program} == (
        set(projection.trust_inventory.paths) - set(projection.configured_program.paths)
    )
    omitted_status = status["trusted_not_in_configured_program"]
    assert omitted_status["total"] == len(projection.trusted_not_in_configured_program)
    assert omitted_status["omitted_count"] == max(0, omitted_status["total"] - 50)
    assert all(item["reason"] == "excluded_by_native_config" for item in omitted_status["items"])

    omitted = next(
        path for path in projection.trusted_not_in_configured_program if Path(path.path).suffix.lower() == ".mjs"
    )
    inferred = probe_inferred_path_support(
        config,
        ROOT,
        configured_entry_path=DECLARATION_OWNER,
        candidate_path=omitted.path,
    )
    assert inferred.path == omitted.path
    assert inferred.project_kind == "inferred"
    assert inferred.selected_config_path is None
    assert inferred.engine_owned is True
    assert inferred.service_supported is True
    assert inferred.configured_program_before == projection.configured_program.paths
    assert inferred.configured_program_after == projection.configured_program.paths
    assert inferred.supported_without_scope_expansion is True


def test_real_publish_diagnostics_never_names_a_document_version(config: TypeScriptAdapterConfig) -> None:
    """Confirmed locked behavior grounding the unversioned-diagnostics repair:
    the pinned ``typescript-language-server``'s ``publishDiagnostics`` never
    names a document ``version``.  That is exactly why the adapter cannot
    correlate a debounced publication by version and must instead causally
    drain tracking, ``didClose``, then ``didOpen`` on a full-text change.
    This waits deterministically for whatever publication the real server
    sends and asserts on its structure, not on any timing-dependent content.
    """

    publications: queue.Queue[Mapping[str, Any]] = queue.Queue()

    def on_notification(method: str, params: Any) -> None:
        if method == "textDocument/publishDiagnostics" and isinstance(params, Mapping):
            publications.put(params)

    process = LanguageServerSubprocessLauncher.get_instance().launch(
        config.command,
        cwd=ROOT,
        env={"PATH": str(config.node_path.parent)},
    )
    assert process.stdout is not None
    assert process.stdin is not None
    client = SyncLspClient(
        process.stdout,
        process.stdin,
        request_timeout=20.0,
        notification_handler=on_notification,
        request_handlers={
            "client/registerCapability": lambda _params: None,
            "window/workDoneProgress/create": lambda _params: None,
            "workspace/configuration": lambda params: [{} for _item in (params or {}).get("items", [])],
            "workspace/executeClientCommand": lambda _params: [],
            "workspace/applyEdit": lambda _params: {
                "applied": False,
                "failureReason": "real adapter test is read-only",
            },
        },
    )
    try:
        client.start()
        client.request("initialize", config.initialize_params(ROOT), timeout=15.0)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        path = ROOT / DECLARATION_OWNER
        uri = _path_uri(path)
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": config.language_id(path),
                    "version": 1,
                    "text": path.read_text(encoding="utf-8"),
                }
            },
        )
        deadline_publications: list[Mapping[str, Any]] = []
        try:
            while True:
                publication = publications.get(timeout=20.0)
                deadline_publications.append(publication)
                if publication.get("uri") == uri:
                    break
        except queue.Empty as error:
            raise AssertionError(
                "the pinned TypeScript server never published diagnostics for the opened document"
            ) from error
        matching = deadline_publications[-1]
        assert "version" not in matching
    finally:
        client.shutdown(timeout=3.0)
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree_with_kill_fallback(
                process,
                2.0,
                "real TypeScript adapter publishDiagnostics probe",
                kill_timeout=2.0,
            )


def test_real_product_adapter_change_to_erroring_document_reports_findings(
    config: TypeScriptAdapterConfig,
    tmp_path: Path,
) -> None:
    """Exercise the production snapshot/diagnostics seam against the locked
    TypeScript server.  Its empty close publication must not consume the
    replacement generation before the true error publication arrives."""

    relative_path = "src/example.ts"
    source = tmp_path / relative_path
    source.parent.mkdir()
    source.write_text('export const answer: string = "ok";\n', encoding="utf-8")
    uri = source.as_uri()
    publications: queue.Queue[Mapping[str, Any]] = queue.Queue()

    def on_notification(method: str, params: Any) -> None:
        if method == "textDocument/publishDiagnostics" and isinstance(params, Mapping):
            publications.put(params)

    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=(relative_path,),
        attribution=NativeProgramAttribution(
            language=LanguageFamily.TYPESCRIPT,
            project_kind=ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=None,
            configured_program_paths=(relative_path,),
        ),
    )
    executor = BoundedLspExecutor(queue_capacity=8, name="real-typescript-diagnostics-barrier")
    state = LspState()
    adapter = _WorkspaceLanguageAdapter(
        workspace_root=tmp_path,
        facts=config.adapter_language_facts(tmp_path),
        runtime_provider=config.runtime_provider(tmp_path),
        executor=executor,
        scope_tracker=ScopeGenerationTracker(projection, max_wait_seconds=20.0),
        lsp_state=state,
        document_witness=PublishedDiagnosticsWitness(),
        operation_lock=threading.RLock(),
        readiness_timeout=20.0,
        notification_handler=on_notification,
    )
    try:
        _initial_snapshot, initial = adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path=relative_path,
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=25.0)
        _wait_for_matching_publication(
            publications,
            uri=uri,
            require_findings=False,
            timeout=20.0,
        )
        adapter.submit_read(
            lambda client: client.request(
                "workspace/symbol",
                {"query": "__serena_light_initial_publication_fence__"},
                timeout=10.0,
            )
        ).result(timeout=15.0)
        assert adapter.diagnostics_snapshot(initial).state is DiagnosticsState.CLEAN

        while not publications.empty():
            publications.get_nowait()
        source.write_text('export const answer: string = 1;\n', encoding="utf-8")
        _changed_snapshot, changed = adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path=relative_path,
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=25.0)
        wire_publication = _wait_for_matching_publication(
            publications,
            uri=uri,
            require_findings=True,
            timeout=20.0,
        )
        assert "version" not in wire_publication
        adapter.submit_read(
            lambda client: client.request(
                "workspace/symbol",
                {"query": "__serena_light_error_publication_fence__"},
                timeout=10.0,
            )
        ).result(timeout=15.0)

        publication = adapter.diagnostics_snapshot(changed)
        assert changed.document_generation == initial.document_generation + 1
        assert publication.state is DiagnosticsState.FINDINGS
        assert publication.generation == changed.document_generation
        assert publication.diagnostics
    finally:
        with suppress(Exception):
            adapter.stop().result(timeout=10.0)
        executor.close()


@pytest.mark.parametrize("trial", range(6))
def test_real_close_barrier_timeout_retries_same_connection_before_reopen_reports_findings(
    config: TypeScriptAdapterConfig,
    tmp_path: Path,
    trial: int,
) -> None:
    """Regression for the audited false-CLEAN counterexample: didClose is
    delivered, the first same-connection ``workspace/willRenameFiles`` close
    barrier fails (TimeoutError), and the exact-version retry must still
    issue a *second* barrier on the *same* client/process before it reopens
    the changed generation.  A predecessor that forgot the undrained-close
    marker across that failure would see nothing left to drain on retry,
    skip the barrier, and reopen immediately -- letting the still in-flight
    empty close publication land on the new generation as a false CLEAN.
    This only injects one failed response at the client seam; the real
    pinned ``typescript-language-server`` process is never restarted.
    """

    relative_path = "src/example.ts"
    source = tmp_path / relative_path
    source.parent.mkdir()
    source.write_text('export const answer: string = "ok";\n', encoding="utf-8")
    uri = source.as_uri()
    publications: queue.Queue[Mapping[str, Any]] = queue.Queue()

    def on_notification(method: str, params: Any) -> None:
        if method == "textDocument/publishDiagnostics" and isinstance(params, Mapping):
            publications.put(params)

    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=(relative_path,),
        attribution=NativeProgramAttribution(
            language=LanguageFamily.TYPESCRIPT,
            project_kind=ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=None,
            configured_program_paths=(relative_path,),
        ),
    )
    executor = BoundedLspExecutor(queue_capacity=8, name=f"real-typescript-close-barrier-retry-{trial}")
    state = LspState()
    adapter = _WorkspaceLanguageAdapter(
        workspace_root=tmp_path,
        facts=config.adapter_language_facts(tmp_path),
        runtime_provider=config.runtime_provider(tmp_path),
        executor=executor,
        scope_tracker=ScopeGenerationTracker(projection, max_wait_seconds=20.0),
        lsp_state=state,
        document_witness=PublishedDiagnosticsWitness(),
        operation_lock=threading.RLock(),
        readiness_timeout=20.0,
        notification_handler=on_notification,
    )
    try:
        _initial_snapshot, original = adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path=relative_path,
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=25.0)
        _wait_for_matching_publication(publications, uri=uri, require_findings=False, timeout=20.0)
        adapter.submit_read(
            lambda client: client.request(
                "workspace/symbol",
                {"query": "__serena_light_close_barrier_initial_fence__"},
                timeout=10.0,
            )
        ).result(timeout=15.0)
        assert adapter.diagnostics_snapshot(original).state is DiagnosticsState.CLEAN

        # Inject exactly one failure into the first close barrier issued from
        # this point on, on the exact client/process this adapter already
        # owns.  No new client, no new process: this is a same-connection
        # retry, not a crash-triggered restart.
        runtime = adapter._runtime
        assert runtime is not None
        client = runtime.client
        real_request = client.request
        real_notify = client.notify
        lifecycle: list[str] = []
        barrier_calls = {"count": 0}

        def flaky_request(method: str, params: object = None, *, timeout: float | None = None) -> Any:
            if method == "workspace/willRenameFiles":
                barrier_calls["count"] += 1
                if barrier_calls["count"] == 1:
                    lifecycle.append("willRenameFiles:fail")
                    raise TimeoutError("synthetic first close-barrier timeout")
                lifecycle.append("willRenameFiles:ok")
            return real_request(method, params, timeout=timeout)

        def logging_notify(method: str, params: object = None) -> None:
            if method in ("textDocument/didOpen", "textDocument/didClose"):
                lifecycle.append(method)
            real_notify(method, params)

        mutable_client = cast(Any, client)
        mutable_client.request = flaky_request
        mutable_client.notify = logging_notify

        while not publications.empty():
            publications.get_nowait()
        source.write_text('export const answer: string = 1;\n', encoding="utf-8")

        failed = adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path=relative_path,
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        )
        with pytest.raises(TimeoutError, match="synthetic first close-barrier timeout"):
            failed.result(timeout=15.0)

        assert barrier_calls["count"] == 1
        assert lifecycle == ["textDocument/didClose", "willRenameFiles:fail"]
        current = state.document(uri)
        assert current is not None
        assert current.version == original.version
        assert current.generation == original.document_generation
        assert uri not in adapter._open_documents
        assert adapter._pending_diagnostics.get(uri) is None
        assert adapter._pending_documents.get(uri) is None
        marker = adapter._undrained_unversioned_closes.get(uri)
        assert marker is not None
        assert marker.close_delivered is True
        assert marker.runtime_token == adapter._runtime_token

        # Exact-version retry on the same connection: must drain via a
        # *second* barrier, must not resend didClose, and must only then
        # admit the changed didOpen.
        _changed_snapshot, changed = adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path=relative_path,
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=25.0)

        assert barrier_calls["count"] == 2
        assert lifecycle == [
            "textDocument/didClose",
            "willRenameFiles:fail",
            "willRenameFiles:ok",
            "textDocument/didOpen",
        ]
        assert uri not in adapter._undrained_unversioned_closes
        assert changed.version == 2
        assert changed.document_generation == original.document_generation + 1

        wire_publication = _wait_for_matching_publication(
            publications,
            uri=uri,
            require_findings=True,
            timeout=20.0,
        )
        assert "version" not in wire_publication
        adapter.submit_read(
            lambda client: client.request(
                "workspace/symbol",
                {"query": "__serena_light_close_barrier_retry_fence__"},
                timeout=10.0,
            )
        ).result(timeout=15.0)

        publication = adapter.diagnostics_snapshot(changed)
        assert publication.state is DiagnosticsState.FINDINGS
        assert publication.generation == changed.document_generation
        assert publication.diagnostics
    finally:
        with suppress(Exception):
            adapter.stop().result(timeout=10.0)
        executor.close()


@pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")
def test_real_pyright_publish_diagnostics_names_an_integer_document_version() -> None:
    """Grounds ``AdapterLanguageFacts.diagnostic_publications_include_version``
    defaulting to ``True`` for Pyright: unlike the pinned TypeScript server,
    the pinned Pyright server's ``publishDiagnostics`` does name an integer
    document ``version``, so its changed-document lifecycle correctly keeps
    the ordinary version-correlated ``didChange`` path rather than the
    TypeScript-only drain/``didClose``/``didOpen`` repair.  This waits
    deterministically for whatever publication the real server sends and
    asserts on its structure, not on any timing-dependent content.
    """

    facts = PyrightFacts.locked()
    publications: queue.Queue[Mapping[str, Any]] = queue.Queue()

    def on_notification(method: str, params: Any) -> None:
        if method == "textDocument/publishDiagnostics" and isinstance(params, Mapping):
            publications.put(params)

    environment = {"PATH": str(Path(facts.command[0]).parent)}
    process = subprocess.Popen(
        facts.command,
        cwd=MS_SWIFT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    client = SyncLspClient(
        process.stdout,
        process.stdin,
        request_timeout=20.0,
        notification_handler=on_notification,
        request_handlers={
            "workspace/configuration": facts.workspace_configuration,
            "workspace/executeClientCommand": lambda _params: [],
            "workspace/applyEdit": lambda _params: {
                "applied": False,
                "failureReason": "real Pyright adapter test is read-only",
            },
        },
    )
    try:
        client.start()
        client.request("initialize", facts.initialize_params(MS_SWIFT), timeout=15.0)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        source = MS_SWIFT / "swift/tuner_plugin/base.py"
        uri = source.as_uri()
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": facts.language_id,
                    "version": 1,
                    "text": source.read_text(encoding="utf-8"),
                }
            },
        )
        collected: list[Mapping[str, Any]] = []
        try:
            while True:
                publication = publications.get(timeout=25.0)
                collected.append(publication)
                if publication.get("uri") == uri:
                    break
        except queue.Empty as error:
            raise AssertionError(
                "the pinned Pyright server never published diagnostics for the opened document"
            ) from error
        matching = collected[-1]
        assert isinstance(matching.get("version"), int)
    finally:
        with suppress(Exception):
            client.shutdown(timeout=2.0)
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
