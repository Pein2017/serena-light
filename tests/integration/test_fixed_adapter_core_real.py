from __future__ import annotations

import threading
from pathlib import Path

import pytest

from serena_light.lsp.adapter import (
    DocumentSymbolReadinessProbe,
    GlobalReadinessWitness,
    LanguageAdapter,
    PublishedDiagnosticsWitness,
)
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.normalize import normalize_document_symbols
from serena_light.lsp.pyright import PyrightFacts
from serena_light.lsp.state import LspState
from serena_light.lsp.typescript import TypeScriptAdapterConfig
from serena_light.workspace.scope import (
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeGenerationTracker,
    ScopeProjection,
)

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.timeout(45)


@pytest.mark.parametrize(
    ("name", "extension", "family"),
    [
        ("python", ".py", LanguageFamily.PYTHON),
        ("typescript", ".ts", LanguageFamily.TYPESCRIPT),
    ],
)
def test_fixed_language_facts_run_through_shared_adapter_core(
    name: str,
    extension: str,
    family: LanguageFamily,
) -> None:
    workspace = ROOT / "tests" / "integration" / "fixtures" / "readiness" / name
    relative = f"src/sentinel{extension}"
    source = workspace / relative
    uri = source.as_uri()
    fixed = PyrightFacts.locked() if family is LanguageFamily.PYTHON else TypeScriptAdapterConfig.locked()
    facts = fixed.adapter_language_facts(workspace)
    runtime_provider = fixed.runtime_provider(workspace)
    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=(relative,),
        attribution=NativeProgramAttribution(
            language=family,
            project_kind=ProjectKind.CONFIGURED,
            selected_config_path="pyrightconfig.json" if family is LanguageFamily.PYTHON else "tsconfig.json",
            configured_program_paths=(relative,),
        ),
    )
    executor = BoundedLspExecutor(queue_capacity=8, name=f"real-{name}")
    adapter = LanguageAdapter(
        workspace_root=workspace,
        facts=facts,
        runtime_provider=runtime_provider,
        executor=executor,
        scope_tracker=ScopeGenerationTracker(projection),
        lsp_state=LspState(),
        document_witness=PublishedDiagnosticsWitness(),
        operation_lock=threading.RLock(),
        readiness_timeout=30.0,
    )
    try:
        target = adapter.open_document(
            relative_path=relative,
            uri=uri,
            version=1,
            text=source.read_text(encoding="utf-8"),
        ).result(timeout=20)
        adapter.probe_document(target, DocumentSymbolReadinessProbe()).result(timeout=35)
        assert adapter.wait_for_document(target, timeout=0).ready

        raw_symbols = adapter.submit_read(
            lambda client: client.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                timeout=15.0,
            )
        ).result(timeout=20)
        symbols = normalize_document_symbols(raw_symbols, document_uri=uri)
        assert symbols
        sentinel = symbols[0].name

        exact = adapter.warm_global(GlobalReadinessWitness(sentinel, uri)).result(timeout=35)
        assert exact
        assert adapter.wait_for_global(timeout=0).ready
    finally:
        adapter.stop().result(timeout=10)
        executor.close(timeout=10)
