from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import MS_INTERPRETER, WorkspacePolicy
from serena_light.workspace.inventory import git_trust_inventory
from serena_light.workspace.scope import ProjectKind

MS_SWIFT = Path("/data/ms-swift")
MS_SITE_PACKAGES = MS_INTERPRETER.parents[1] / "lib" / "python3.12" / "site-packages"
TRANSFORMERS_ROOT = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)

pytestmark = [
    pytest.mark.timeout(90),
    pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT"),
    pytest.mark.external_repo(root=str(TRANSFORMERS_ROOT), snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT"),
]


@contextmanager
def _real_pyright(facts: PyrightFacts) -> Iterator[SyncLspClient]:
    environment = os.environ.copy()
    environment["PATH"] = str(Path(facts.command[0]).parent)
    environment.pop("NODE_PATH", None)
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
        request_handlers={
            "workspace/configuration": facts.workspace_configuration,
            "workspace/executeClientCommand": lambda _params: [],
            "workspace/applyEdit": lambda _params: {
                "applied": False,
                "failureReason": "real Pyright adapter test is read-only",
            },
        },
    )
    client.start()
    try:
        yield client
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


def _definition(
    client: SyncLspClient,
    facts: PyrightFacts,
    source: Path,
    occurrence: str,
) -> tuple[Any, ...]:
    text = source.read_text(encoding="utf-8")
    offset = text.index(occurrence)
    line = text.count("\n", 0, offset)
    line_start = text.rfind("\n", 0, offset) + 1
    client.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": source.as_uri(),
                "languageId": facts.language_id,
                "version": 1,
                "text": text,
            }
        },
    )
    result = client.request(
        facts.definition_method,
        {
            "textDocument": {"uri": source.as_uri()},
            "position": {"line": line, "character": offset - line_start},
        },
    )
    if result is None:
        return ()
    if isinstance(result, list):
        return tuple(result)
    return (result,)


def test_real_pyright_definitions_use_ms_interpreter_and_are_read_only_external() -> None:
    facts = PyrightFacts.locked()
    policy = WorkspacePolicy()
    identity = policy.resolve_activation(MS_SWIFT)

    with _real_pyright(facts) as client:
        initialize_result = client.request("initialize", facts.initialize_params(MS_SWIFT))
        providers = facts.provider_facts(initialize_result)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": {}})

        generation_raw = _definition(
            client,
            facts,
            MS_SWIFT / "swift/infer_engine/lmdeploy_engine.py",
            "GenerationConfig",
        )
        peft_raw = _definition(
            client,
            facts,
            MS_SWIFT / "swift/tuner_plugin/base.py",
            "PeftModel",
        )

    assert providers.raw.definition
    assert providers.derived.find_declaration
    assert not providers.derived.find_implementations
    generation = facts.classify_definition_locations(
        generation_raw,
        classify=lambda path: policy.classify_semantic_location(identity, path),
    )
    peft = facts.classify_definition_locations(
        peft_raw,
        classify=lambda path: policy.classify_semantic_location(identity, path),
    )
    transformers_root = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)
    peft_root = (MS_SITE_PACKAGES / "peft").resolve(strict=True)
    assert generation
    assert all(item.semantic_location.read_only_external for item in generation)
    assert any(item.semantic_location.path.is_relative_to(transformers_root) for item in generation)
    assert peft
    assert all(item.semantic_location.read_only_external for item in peft)
    assert any(item.semantic_location.path.is_relative_to(peft_root) for item in peft)


def test_real_native_program_attribution_selects_workspace_default_without_overlay() -> None:
    facts = PyrightFacts.locked()
    inventory = git_trust_inventory(MS_SWIFT)

    evidence = facts.owned_files_evidence(MS_SWIFT, timeout=25.0)
    projection = facts.attribute_program(MS_SWIFT, inventory.paths, timeout=25.0)

    assert evidence.project_kind is ProjectKind.WORKSPACE_DEFAULT
    assert evidence.selected_config_path is None
    assert evidence.owned_files
    assert projection.project_kind is ProjectKind.WORKSPACE_DEFAULT
    assert projection.selected_config_path is None
    assert projection.configured_program.count == len(evidence.owned_files)
    assert projection.compatible
    assert projection.overlay_generated is False
