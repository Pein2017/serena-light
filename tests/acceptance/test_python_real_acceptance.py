"""Real Python acceptance for OpenSpec tasks 9.2 and 9.4.

These tests intentionally use the production ``WorkspaceRuntime`` composition
root and its locked Pyright process.  They do not replace native-program
attribution, LSP transport, readiness, diagnostics, or edit authorization with
test doubles.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import (
    MS_INTERPRETER,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceKind,
    WorkspacePolicy,
)
from serena_light.workspace.inventory import git_trust_inventory
from serena_light.workspace.runtime import WorkspaceRuntime
from serena_light.workspace.scope import LanguageFamily

MS_SITE_PACKAGES = MS_INTERPRETER.parents[1] / "lib" / "python3.12" / "site-packages"
TRANSFORMERS_ROOT = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)
COORDEXP = Path("/data/CoordExp")
MS_SWIFT = Path("/data/ms-swift")
RSS_LIMIT_BYTES = 8 * 1024**3

pytestmark = pytest.mark.timeout(180)


@dataclass
class _ProcessEvidence:
    peak_tree_rss_bytes: int = 0
    cleanup_ok: bool = False
    cleanup_live: tuple[str, ...] = ()


class _ProcessSampler:
    def __init__(self) -> None:
        self.evidence = _ProcessEvidence()
        self._parent = psutil.Process()
        self._baseline: dict[int, float] = {}
        try:
            baseline_children = self._parent.children(recursive=True)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            baseline_children = []
        for child in baseline_children:
            try:
                self._baseline[child.pid] = child.create_time()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
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
        live = self._live_observed()
        self.evidence.cleanup_ok = not live
        self.evidence.cleanup_live = tuple(self._process_description(pid) for pid in live)

    def _sample(self) -> None:
        while not self._stop.is_set():
            total = self._safe_rss(self._parent)
            try:
                children = self._parent.children(recursive=True)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                children = []
            for child in children:
                try:
                    create_time = child.create_time()
                    command = child.cmdline()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                if not any(
                    part.endswith(
                        ("/pyright/langserver.index.js", "/typescript-language-server/lib/cli.mjs")
                    )
                    for part in command
                ):
                    continue
                if self._baseline.get(child.pid) == create_time:
                    continue
                self._observed[child.pid] = create_time
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

    @staticmethod
    def _process_description(pid: int) -> str:
        try:
            process = psutil.Process(pid)
            return f"pid={pid} cmd={process.cmdline()}"
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return f"pid={pid} disappeared"


@contextmanager
def _real_runtime(
    root: Path,
    *,
    python_environment: str | None = None,
) -> Iterator[tuple[WorkspaceRuntime, _ProcessEvidence]]:
    policy = WorkspacePolicy()
    sampler = _ProcessSampler()
    runtime: WorkspaceRuntime | None = None
    sampler.start()
    try:
        identity = policy.resolve_activation(root, python_environment=python_environment)
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


@pytest.mark.external_repo(root=str(COORDEXP), snapshot_env="SERENA_LIGHT_COORDEXP_SNAPSHOT")
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


@pytest.mark.external_repo(root=str(COORDEXP), snapshot_env="SERENA_LIGHT_COORDEXP_SNAPSHOT")
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
    assert process.cleanup_ok, f"real CoordExp acceptance left owned descendants: {process.cleanup_live}"
    assert process.peak_tree_rss_bytes < RSS_LIMIT_BYTES
    _assert_global_symbol(result, "PipelinePlanner", "public_data/pipeline/planner.py", status=after)
    _assert_current_global_generation(after)


def test_real_pyright_global_lookup_refreshes_a_previously_opened_external_change(tmp_path: Path) -> None:
    """A sentinel alone must not certify a stale already-open sibling buffer."""

    root = tmp_path / "freshness-workspace"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "pyrightconfig.json").write_text('{"include": ["*.py"]}\n', encoding="utf-8")
    (root / "a_sentinel.py").write_text("class StableSentinel:\n    pass\n", encoding="utf-8")
    renamed = root / "z_renamed.py"
    renamed.write_text("class OldSymbol:\n    pass\n", encoding="utf-8")

    with _real_runtime(root) as (runtime, process):
        # Keep this distinct file open before its out-of-band rewrite.  The
        # global warm-up below deterministically chooses a_sentinel.py first.
        runtime.load_document_symbols("z_renamed.py")
        renamed.write_text("class NewSymbol:\n    pass\n", encoding="utf-8")

        result = _dict(runtime.find_symbol("NewSymbol").to_dict())
        status = _adapter_status(runtime, LanguageFamily.PYTHON)

    assert process.cleanup_ok, f"real Pyright acceptance left owned descendants: {process.cleanup_live}"
    if result["ok"] is True:
        _assert_global_symbol(result, "NewSymbol", "z_renamed.py", status=status)
    else:
        failure = _dict(result["error"])
        assert failure["code"] == "NOT_READY", result


@pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")
@pytest.mark.external_repo(root=str(TRANSFORMERS_ROOT), snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT")
def test_ms_swift_definition_and_diagnostics_use_conda_ms(record_property: Any) -> None:
    source = "swift/infer_engine/lmdeploy_engine.py"
    exact_transformers_root = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)
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
    assert process.cleanup_ok, f"real ms-swift acceptance left owned descendants: {process.cleanup_live}"
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


def test_explicit_conda_environment_resolves_its_installed_package(tmp_path: Path) -> None:
    root = tmp_path / "llm-environment-workspace"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "pyrightconfig.json").write_text('{"include": ["example.py"]}\n', encoding="utf-8")
    (root / "example.py").write_text(
        "from torchtune.config import parse as selected\n",
        encoding="utf-8",
    )
    interpreter = Path("/root/miniconda3/envs/llm-framework-study/bin/python")
    site_packages = interpreter.parents[1] / "lib" / "python3.12" / "site-packages"
    package_root = Path(
        "/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages/torchtune"
    ).resolve(strict=True)
    policy = WorkspacePolicy()
    non_git_identity = policy.resolve_activation(
        site_packages,
        python_environment="llm-framework-study",
    )
    assert non_git_identity.root == site_packages.resolve(strict=True)
    assert non_git_identity.kind is WorkspaceKind.NON_GIT_READ_ONLY
    assert non_git_identity.python_environment == "llm-framework-study"
    with pytest.raises(WorkspaceError) as read_only:
        policy.authorize_edit(
            non_git_identity,
            package_root / "__init__.py",
            [package_root / "__init__.py"],
        )
    assert read_only.value.data.code is WorkspaceErrorCode.READ_ONLY_ROOT

    with _real_runtime(root, python_environment="llm-framework-study") as (runtime, process):
        runtime_identity = _dict(runtime.status()["identity"])
        definition = _dict(
            runtime.find_declaration("example.py", r"import parse as (selected)").to_dict()
        )
        diagnostics = _dict(
            runtime.get_diagnostics_for_file("example.py", timeout_seconds=15.0).to_dict()
        )

    assert process.cleanup_ok, f"real Pyright acceptance left owned descendants: {process.cleanup_live}"
    assert runtime_identity["python_environment"] == "llm-framework-study"
    assert runtime_identity["python_interpreter"] == str(interpreter)
    assert definition["ok"] is True, definition
    locations = [_dict(item) for item in _list(_dict(definition["data"])["locations"])]
    assert any(
        Path(item["absolute_path"]).is_relative_to(package_root)
        and item["location_kind"] == "read_only_external"
        and item["read_only_external"] is True
        for item in locations
    ), locations
    assert diagnostics["ok"] is True, diagnostics
    engine = _dict(_dict(diagnostics["data"])["engine"])
    assert engine["python_environment"] == "llm-framework-study"
    assert engine["interpreter"] == str(interpreter)


def test_exact_llm_site_packages_is_semantically_navigable_and_read_only(record_property: Any) -> None:
    """An exact non-Git root is usable when Pyright and trust own the same paths."""

    interpreter = Path("/root/miniconda3/envs/llm-framework-study/bin/python")
    site_packages = interpreter.parents[1] / "lib" / "python3.12" / "site-packages"
    target = "torchtune/config/_parse.py"
    target_path = site_packages / target
    original = target_path.read_bytes()

    with _real_runtime(site_packages, python_environment="llm-framework-study") as (runtime, process):
        status = _dict(runtime.status())
        projection = runtime.projections[LanguageFamily.PYTHON]
        overview = _dict(runtime.get_symbols_overview(target, max_depth=0).to_dict())
        symbol = _dict(runtime.find_symbol("parse", relative_path=target).to_dict())
        edit = _dict(
            runtime.replace_symbol_body(
                "parse",
                target,
                "def parse(recipe_main): ...",
                hashlib.sha256(original).hexdigest(),
            ).to_dict()
        )

    record_property("peak_tree_rss_bytes", process.peak_tree_rss_bytes)
    record_property("configured_program_count", projection.configured_program.count)
    assert process.cleanup_ok, f"real non-Git acceptance left owned descendants: {process.cleanup_live}"
    assert target_path.read_bytes() == original
    assert _dict(status["identity"])["kind"] == "non_git_read_only"
    assert _dict(status["identity"])["python_environment"] == "llm-framework-study"
    assert "python" in _dict(status["adapters"])
    assert "python" not in _dict(status["unavailable_language_families"])
    assert projection.compatible is True
    assert projection.configured_program.paths == projection.trust_inventory.paths
    assert overview["ok"] is True, overview
    overview_names = {item["name"] for item in _list(_dict(overview["data"])["symbols"])}
    assert "parse" in overview_names
    assert symbol["ok"] is True, symbol
    assert _dict(_dict(symbol["data"])["symbol"])["name_path"] == "parse"
    assert edit["ok"] is False, edit
    assert _dict(edit["error"])["code"] == "READ_ONLY_ROOT"


@pytest.mark.external_repo(root=str(TRANSFORMERS_ROOT), snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT")
def test_transformers_semantic_liveness_and_all_edits_read_only(record_property: Any) -> None:
    exact_root = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)
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

        retries = 0
        symbol: dict[str, Any] | None = None
        status: dict[str, Any] | None = None
        for attempt in range(3):
            candidate = _dict(runtime.find_symbol("Qwen2VLForConditionalGeneration").to_dict())
            candidate_status = _adapter_status(runtime, LanguageFamily.PYTHON)
            if candidate["ok"] is True:
                symbol = candidate
                status = candidate_status
                break

            failure = _dict(candidate["error"])
            assert failure["code"] == "NOT_READY", candidate
            retry = _dict(failure["retry"])
            envelope_generations = _dict(candidate["generations"])
            status_generations = _dict(candidate_status["generations"])
            assert retry["retryable"] is True, candidate
            assert candidate_status["phase"] in {
                "starting",
                "document_ready",
                "global_warming",
                "degraded",
            }, candidate_status
            assert envelope_generations["program"] == status_generations["program"], candidate
            assert envelope_generations["index"] == status_generations["index"], candidate
            assert int(status_generations["program"]) >= 1, candidate_status
            assert int(status_generations["index"]) <= int(status_generations["program"]), candidate_status
            retries = attempt + 1

        assert symbol is not None and status is not None, (
            "expected success within three production calls, allowing only typed NOT_READY before success"
        )
        edit = _dict(
            runtime.replace_symbol_body(
                "Qwen2VLForConditionalGeneration",
                target,
                "class Qwen2VLForConditionalGeneration: ...",
                "0" * 64,
            ).to_dict()
        )

    record_property("not_ready_retries", retries)
    record_property("peak_tree_rss_bytes", process.peak_tree_rss_bytes)
    record_property("configured_program_count", projection.configured_program.count)
    assert process.cleanup_ok, f"real transformers acceptance left owned descendants: {process.cleanup_live}"
    assert edit["ok"] is False, edit
    assert _dict(edit["error"])["code"] == "READ_ONLY_ROOT"
    _assert_global_symbol(symbol, "Qwen2VLForConditionalGeneration", target, status=status)
    _assert_current_global_generation(status)


@pytest.mark.performance_external
@pytest.mark.external_repo(root=str(TRANSFORMERS_ROOT), snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT")
def test_transformers_first_production_readiness_attempt_performance(record_property: Any) -> None:
    exact_root = (MS_SITE_PACKAGES / "transformers").resolve(strict=True)
    assert exact_root == TRANSFORMERS_ROOT.resolve(strict=True)

    with _real_runtime(exact_root) as (runtime, process):
        started = time.monotonic()
        symbol = _dict(runtime.find_symbol("Qwen2VLForConditionalGeneration").to_dict())
        elapsed_seconds = time.monotonic() - started
        status = _adapter_status(runtime, LanguageFamily.PYTHON)

    record_property("first_production_elapsed_seconds", elapsed_seconds)
    record_property("first_production_result", symbol["ok"])
    record_property("first_production_phase", status["phase"])
    assert process.cleanup_ok, (
        f"real transformers performance acceptance left owned descendants: {process.cleanup_live}"
    )
    assert symbol["ok"] is True, {
        "result": symbol,
        "phase": status["phase"],
        "generations": status["generations"],
    }
    _assert_global_symbol(
        symbol,
        "Qwen2VLForConditionalGeneration",
        "models/qwen2_vl/modeling_qwen2_vl.py",
        status=status,
    )


@dataclass(frozen=True, slots=True)
class _LatencyCase:
    root: Path
    snapshot_env: str
    relative_path: str
    symbol_name: str


_LATENCY_CASES = (
    _LatencyCase(COORDEXP, "SERENA_LIGHT_COORDEXP_SNAPSHOT", "public_data/pipeline/planner.py", "PipelinePlanner"),
    _LatencyCase(MS_SWIFT, "SERENA_LIGHT_MS_SWIFT_SNAPSHOT", "swift/infer_engine/lmdeploy_engine.py", "LmdeployEngine"),
)

_LATENCY_REPEATS = 2


def _assert_scoped_symbol_present(result: Mapping[str, Any], case: _LatencyCase) -> None:
    data = _dict(result["data"])
    symbol = _dict(data["symbol"])
    assert data["relative_path"] == case.relative_path, data
    assert symbol["name"] == case.symbol_name, symbol


def _assert_global_symbol_present(result: Mapping[str, Any], case: _LatencyCase) -> None:
    data = _dict(result["data"])
    symbols = [_dict(item) for item in _list(data["symbols"])]
    assert any(item["name"] == case.symbol_name and item["relative_path"] == case.relative_path for item in symbols), (
        symbols
    )


@pytest.mark.performance_external
@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            marks=pytest.mark.external_repo(root=str(case.root), snapshot_env=case.snapshot_env),
            id=case.root.name,
        )
        for case in _LATENCY_CASES
    ],
)
def test_navigation_and_diagnostics_per_call_latency_is_observation_only(
    case: _LatencyCase, record_property: Any
) -> None:
    """Record two-sample minimum/maximum wall-clock latency; there is no pass threshold.

    Correctness of every response remains mandatory: each call must still
    resolve through the production ``WorkspaceRuntime`` boundary with
    ``ok is True``.
    """

    with _real_runtime(case.root) as (runtime, process):
        calls: tuple[
            tuple[
                str,
                Callable[[], Mapping[str, Any]],
                Callable[[Mapping[str, Any], _LatencyCase], None] | None,
            ],
            ...,
        ] = (
            (
                "find_symbol_global",
                lambda: _dict(runtime.find_symbol(case.symbol_name).to_dict()),
                _assert_global_symbol_present,
            ),
            (
                "find_symbol_scoped",
                lambda: _dict(runtime.find_symbol(case.symbol_name, relative_path=case.relative_path).to_dict()),
                _assert_scoped_symbol_present,
            ),
            (
                "get_symbols_overview",
                lambda: _dict(runtime.get_symbols_overview(case.relative_path).to_dict()),
                None,
            ),
            (
                "get_diagnostics_for_file",
                lambda: _dict(runtime.get_diagnostics_for_file(case.relative_path, timeout_seconds=15.0).to_dict()),
                None,
            ),
        )
        samples: dict[str, list[float]] = {name: [] for name, _call, _check in calls}
        for _repeat in range(_LATENCY_REPEATS):
            for name, call, check in calls:
                started = time.monotonic()
                result = call()
                elapsed_seconds = time.monotonic() - started
                assert result["ok"] is True, {"case": case.root.name, "call": name, "result": result}
                if check is not None:
                    check(result, case)
                samples[name].append(elapsed_seconds)
        status = _adapter_status(runtime, LanguageFamily.PYTHON)

    assert process.cleanup_ok, (
        f"real {case.root.name} latency acceptance left owned descendants: {process.cleanup_live}"
    )
    _assert_current_global_generation(status)
    record_property(f"{case.root.name}_peak_tree_rss_bytes", process.peak_tree_rss_bytes)
    for name, latencies in samples.items():
        record_property(f"{case.root.name}_{name}_minimum_latency_seconds", min(latencies))
        record_property(f"{case.root.name}_{name}_maximum_latency_seconds", max(latencies))
