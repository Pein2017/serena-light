"""Real Python acceptance for OpenSpec tasks 9.2 and 9.4.

These tests intentionally use the production ``WorkspaceRuntime`` composition
root and its locked Pyright process.  They do not replace native-program
attribution, LSP transport, readiness, diagnostics, or edit authorization with
test doubles.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import MS_INTERPRETER, TRANSFORMERS_ROOT, PinnedMsRoots, WorkspacePolicy
from serena_light.workspace.inventory import git_trust_inventory
from serena_light.workspace.runtime import WorkspaceRuntime
from serena_light.workspace.scope import LanguageFamily

COORDEXP = Path("/data/CoordExp")
MS_SWIFT = Path("/data/ms-swift")
RSS_LIMIT_BYTES = 8 * 1024**3
# Global queries now include the mandatory no-cache Git freshness pass.  The
# CoordExp root's untracked-aware `git ls-files` scan is about five seconds on
# this host, so retain the original 30-second LSP budget plus bounded host-load
# headroom instead of misclassifying the required freshness work as a hang.
READINESS_LIMIT_SECONDS = 40.0

pytestmark = pytest.mark.timeout(180)


@dataclass
class _ProcessEvidence:
    peak_tree_rss_bytes: int = 0
    cleanup_ok: bool = False


class _ProcessSampler:
    def __init__(self) -> None:
        self.evidence = _ProcessEvidence()
        self._parent = psutil.Process()
        self._stop = threading.Event()
        self._observed: dict[int, float] = {}
        self._thread = threading.Thread(target=self._sample, name="python-real-acceptance-rss", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        deadline = time.monotonic() + 5.0
        while self._live_observed() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.evidence.cleanup_ok = not self._live_observed()

    def _sample(self) -> None:
        while not self._stop.is_set():
            total = self._safe_rss(self._parent)
            try:
                children = self._parent.children(recursive=True)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                children = []
            for child in children:
                try:
                    self._observed[child.pid] = child.create_time()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                total += self._safe_rss(child)
            self.evidence.peak_tree_rss_bytes = max(self.evidence.peak_tree_rss_bytes, total)
            self._stop.wait(0.05)

    @staticmethod
    def _safe_rss(process: psutil.Process) -> int:
        try:
            return process.memory_info().rss
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return 0

    def _live_observed(self) -> tuple[int, ...]:
        live: list[int] = []
        for pid, create_time in self._observed.items():
            try:
                process = psutil.Process(pid)
                if process.create_time() == create_time and process.status() != psutil.STATUS_ZOMBIE:
                    live.append(pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return tuple(sorted(live))


@contextmanager
def _real_runtime(root: Path) -> Iterator[tuple[WorkspaceRuntime, _ProcessEvidence]]:
    roots = PinnedMsRoots.resolve(MS_INTERPRETER)
    policy = WorkspacePolicy(ms_roots=roots)
    sampler = _ProcessSampler()
    runtime: WorkspaceRuntime | None = None
    sampler.start()
    try:
        identity = policy.resolve_activation(root)
        runtime = WorkspaceRuntime(identity, path_policy=policy, future_timeout=35.0)
        yield runtime, sampler.evidence
    finally:
        if runtime is not None:
            runtime.stop()
        sampler.stop()


def _dict(value: object) -> dict[str, Any]:
    assert isinstance(value, Mapping), value
    result: dict[str, Any] = {}
    for key, item in value.items():
        assert isinstance(key, str), key
        result[key] = item
    return result


def _list(value: object) -> list[Any]:
    assert isinstance(value, Sequence) and not isinstance(value, str | bytes), value
    return list(value)


def _adapter_status(runtime: WorkspaceRuntime, family: LanguageFamily) -> dict[str, Any]:
    status = _dict(runtime.status())
    adapters = _dict(status["adapters"])
    return _dict(adapters[family.value])


def _assert_current_global_generation(adapter: Mapping[str, Any]) -> None:
    generations = _dict(adapter["generations"])
    assert generations["program"] >= 1
    assert generations["index"] == generations["program"]
    assert adapter["phase"] == "ready"


def _assert_global_symbol(
    result: Mapping[str, Any],
    name: str,
    relative_path: str,
    *,
    status: Mapping[str, Any],
) -> None:
    assert result["ok"] is True, {
        "result": result,
        "python_phase": status["phase"],
        "python_running": status["running"],
        "python_generations": status["generations"],
        "python_providers": status["raw_providers"],
        "python_transitions": status["transitions"],
    }
    data = _dict(result["data"])
    symbols = [_dict(item) for item in _list(data["symbols"])]
    assert any(item["name"] == name and item["relative_path"] == relative_path for item in symbols), symbols
    adapters = [_dict(item) for item in _list(data["adapters"])]
    python = next(item for item in adapters if _dict(item["adapter"])["language"] == "python")
    generations = _dict(python["generations"])
    assert generations["index"] == generations["program"]
    assert python["global_ready"] is True
    assert python["phase"] == "ready"


def test_coordexp_python_projection_and_ignored_data_pruning(record_property: Any) -> None:
    ignored_prefixes = ("processed_data", "artifacts", "outputs")
    for prefix in ignored_prefixes:
        ignored = subprocess.run(
            ["git", "-C", str(COORDEXP), "check-ignore", "--quiet", prefix],
            check=False,
        )
        assert ignored.returncode == 0, f"expected CoordExp {prefix!r} to be Git-ignored"

    inventory = git_trust_inventory(COORDEXP)
    for prefix in ignored_prefixes:
        assert not inventory.tree.has_prefix(prefix)
    projection = PyrightFacts.locked().attribute_program(COORDEXP, inventory.paths, timeout=25.0)
    assert projection.selected_config_path == "pyrightconfig.json"
    assert projection.compatible is True
    assert projection.overlay_generated is False
    assert projection.configured_program_outside_trust == ()
    assert projection.trusted_not_in_configured_program
    assert {item.reason.value for item in projection.trusted_not_in_configured_program} == {
        "excluded_by_native_config"
    }
    record_property("trust_count", projection.trust_inventory.count)
    record_property("configured_program_count", projection.configured_program.count)


def test_coordexp_python_configured_program_acceptance(record_property: Any) -> None:
    ignored_prefixes = ("processed_data", "artifacts", "outputs")

    with _real_runtime(COORDEXP) as (runtime, process):
        before = _adapter_status(runtime, LanguageFamily.PYTHON)
        assert before["selected_native_config"] == "pyrightconfig.json"
        assert before["scope_compatible"] is True
        assert before["overlay_generated"] is False
        assert _list(_dict(before["configured_program_outside_trust"])["items"]) == []
        differences = [
            _dict(item) for item in _list(_dict(before["trusted_not_in_configured_program"])["items"])
        ]
        assert differences
        assert {item["reason"] for item in differences} == {"excluded_by_native_config"}
        for prefix in ignored_prefixes:
            assert not runtime.inventory.tree.has_prefix(prefix)

        started = time.monotonic()
        result = _dict(runtime.find_symbol("PipelinePlanner").to_dict())
        readiness_seconds = time.monotonic() - started
        after = _adapter_status(runtime, LanguageFamily.PYTHON)

    record_property("global_readiness_seconds", readiness_seconds)
    record_property("peak_tree_rss_bytes", process.peak_tree_rss_bytes)
    record_property("trust_count", _dict(before["trust_inventory"])["count"])
    record_property("configured_program_count", _dict(before["configured_program"])["count"])
    assert process.cleanup_ok, "real CoordExp acceptance left an owned LSP descendant"
    assert process.peak_tree_rss_bytes < RSS_LIMIT_BYTES
    assert readiness_seconds <= READINESS_LIMIT_SECONDS
    _assert_global_symbol(result, "PipelinePlanner", "public_data/pipeline/planner.py", status=after)
    _assert_current_global_generation(after)


def test_ms_swift_definition_and_diagnostics_use_conda_ms(record_property: Any) -> None:
    source = "swift/infer_engine/lmdeploy_engine.py"
    exact_transformers_root = (PinnedMsRoots.resolve(MS_INTERPRETER).purelib / "transformers").resolve(strict=True)
    assert exact_transformers_root == TRANSFORMERS_ROOT.resolve(strict=True)

    with _real_runtime(MS_SWIFT) as (runtime, process):
        definition = _dict(
            runtime.find_declaration(
                source,
                r"from transformers import (GenerationConfig)",
            ).to_dict()
        )
        diagnostics = _dict(runtime.get_diagnostics_for_file(source, timeout_seconds=15.0).to_dict())
        status = _adapter_status(runtime, LanguageFamily.PYTHON)

    record_property("peak_tree_rss_bytes", process.peak_tree_rss_bytes)
    assert process.cleanup_ok, "real ms-swift acceptance left an owned LSP descendant"
    assert definition["ok"] is True, definition
    locations = [_dict(item) for item in _list(_dict(definition["data"])["locations"])]
    assert any(
        Path(item["absolute_path"]).is_relative_to(exact_transformers_root)
        and item["location_kind"] == "read_only_external"
        and item["read_only_external"] is True
        for item in locations
    ), locations
    assert diagnostics["ok"] is True, diagnostics
    diagnostics_data = _dict(diagnostics["data"])
    assert diagnostics_data["state"] in {"clean", "findings"}
    engine = _dict(diagnostics_data["engine"])
    assert engine["name"] == "pyright"
    assert engine["interpreter"] == str(MS_INTERPRETER)
    assert _dict(status["engine"])["interpreter"] == str(MS_INTERPRETER)


def test_transformers_projection_global_symbol_and_all_edits_read_only(record_property: Any) -> None:
    exact_root = (PinnedMsRoots.resolve(MS_INTERPRETER).purelib / "transformers").resolve(strict=True)
    assert exact_root == TRANSFORMERS_ROOT.resolve(strict=True)
    target = "models/qwen2_vl/modeling_qwen2_vl.py"

    with _real_runtime(exact_root) as (runtime, process):
        projection = runtime.projections[LanguageFamily.PYTHON]
        assert projection.project_kind.value == "workspace_default"
        assert projection.selected_config_path is None
        assert projection.compatible is True
        assert projection.overlay_generated is False
        assert projection.configured_program.paths == projection.trust_inventory.paths
        assert target in projection.configured_program.paths

        started = time.monotonic()
        symbol = _dict(runtime.find_symbol("Qwen2VLForConditionalGeneration").to_dict())
        readiness_seconds = time.monotonic() - started
        status = _adapter_status(runtime, LanguageFamily.PYTHON)
        edit = _dict(
            runtime.replace_symbol_body(
                "Qwen2VLForConditionalGeneration",
                target,
                "class Qwen2VLForConditionalGeneration: ...",
                "0" * 64,
            ).to_dict()
        )

    record_property("global_readiness_seconds", readiness_seconds)
    record_property("peak_tree_rss_bytes", process.peak_tree_rss_bytes)
    record_property("configured_program_count", projection.configured_program.count)
    assert process.cleanup_ok, "real transformers acceptance left an owned LSP descendant"
    assert readiness_seconds <= READINESS_LIMIT_SECONDS
    assert edit["ok"] is False, edit
    assert _dict(edit["error"])["code"] == "READ_ONLY_ROOT"
    _assert_global_symbol(symbol, "Qwen2VLForConditionalGeneration", target, status=status)
    _assert_current_global_generation(status)
