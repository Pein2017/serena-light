from __future__ import annotations

import threading
import time

import pytest

from serena_light.workspace.scope import (
    DifferenceReason,
    FileChangeType,
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ReadinessCode,
    ScopeCode,
    ScopeDifference,
    ScopeGenerationTracker,
    ScopeProjection,
    WatchedFileEvent,
)


def _projection(
    *,
    trust: tuple[str, ...] = ("omitted/manual.py", "src/helper.py", "src/main.py"),
    program: tuple[str, ...] = ("src/helper.py", "src/main.py"),
) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=trust,
        attribution=NativeProgramAttribution(
            language=LanguageFamily.PYTHON,
            project_kind=ProjectKind.CONFIGURED,
            selected_config_path="pyrightconfig.json",
            configured_program_paths=program,
        ),
    )


def test_compatible_projection_has_stable_reasons_and_order_independent_digests() -> None:
    first = _projection()
    second = _projection(
        trust=("src/main.py", "omitted/manual.py", "src/helper.py", "README.md", "src/main.py"),
        program=("src/main.py", "src/helper.py", "package.json"),
    )

    assert first == second
    assert first.compatible
    assert first.error is None
    assert first.overlay_generated is False
    assert first.trust_inventory.count == 3
    assert first.configured_program.count == 2
    assert first.trust_inventory.sha256 == second.trust_inventory.sha256
    assert first.trusted_not_in_configured_program == (
        ScopeDifference("omitted/manual.py", DifferenceReason.EXCLUDED_BY_NATIVE_CONFIG),
    )
    difference = first.trusted_not_in_configured_program[0]
    assert (difference.path, difference.reason) == (
        "omitted/manual.py",
        DifferenceReason.EXCLUDED_BY_NATIVE_CONFIG,
    )


def test_supported_program_path_outside_trust_is_scope_incompatible() -> None:
    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=("src/main.ts",),
        attribution=NativeProgramAttribution(
            language=LanguageFamily.TYPESCRIPT,
            project_kind=ProjectKind.CONFIGURED,
            selected_config_path="tsconfig.json",
            configured_program_paths=("src/main.ts", "ignored-generated/hidden.ts", "node_modules/lib.d.ts.map"),
        ),
        outside_trust_reasons={"ignored-generated/hidden.ts": DifferenceReason.GIT_IGNORED},
    )

    assert not projection.compatible
    assert projection.configured_program.paths == ("ignored-generated/hidden.ts", "src/main.ts")
    assert projection.configured_program_outside_trust[0].reason is DifferenceReason.GIT_IGNORED
    assert projection.error is not None
    assert projection.error.code is ScopeCode.SCOPE_INCOMPATIBLE
    assert projection.error.paths == ("ignored-generated/hidden.ts",)


def test_workspace_default_omission_has_engine_reason() -> None:
    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=("loose.py",),
        attribution=NativeProgramAttribution(
            language=LanguageFamily.PYTHON,
            project_kind=ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=None,
            configured_program_paths=(),
        ),
    )

    assert (
        projection.trusted_not_in_configured_program[0].reason is DifferenceReason.OMITTED_BY_ENGINE_WORKSPACE_PROGRAM
    )


def test_projection_rejects_absolute_or_parent_escaping_paths() -> None:
    for path in ("/data/project/main.py", "../other/main.py"):
        with pytest.raises(ValueError, match="normalized"):
            NativeProgramAttribution(
                language=LanguageFamily.PYTHON,
                project_kind=ProjectKind.WORKSPACE_DEFAULT,
                selected_config_path=None,
                configured_program_paths=(path,),
            )


def test_watcher_events_separate_configured_and_omitted_generations_without_expansion() -> None:
    tracker = ScopeGenerationTracker(_projection())
    assert tracker.observe_configured_program(1)
    assert tracker.observe_path("omitted/manual.py", 0)
    original_program = tracker.projection.configured_program

    omitted = tracker.apply_did_change_watched_files([WatchedFileEvent("omitted/manual.py", FileChangeType.CHANGED)])
    assert omitted.trust_inventory_changed
    assert not omitted.configured_program_invalidated
    assert omitted.after.trust_inventory == 1
    assert omitted.after.configured_program == 1
    assert tracker.wait_for_configured_program(0).code is ReadinessCode.READY
    assert tracker.wait_for_path("omitted/manual.py", 0).code is ReadinessCode.NOT_READY

    configured = tracker.apply_did_change_watched_files([WatchedFileEvent("src/main.py", FileChangeType.CREATED)])
    assert configured.configured_program_invalidated
    assert configured.after.configured_program == 2
    assert tracker.wait_for_configured_program(0).code is ReadinessCode.NOT_READY
    assert tracker.projection.configured_program is original_program
    assert "omitted/manual.py" not in tracker.projection.configured_program.paths


def test_config_change_invalidates_program_but_does_not_reinterpret_membership() -> None:
    tracker = ScopeGenerationTracker(_projection())
    assert tracker.observe_configured_program(1)

    transition = tracker.apply_did_change_watched_files(
        [WatchedFileEvent("pyrightconfig.json", FileChangeType.CHANGED)]
    )

    assert transition.configured_program_invalidated
    assert transition.after.trust_inventory == 0
    assert transition.after.configured_program == 2
    assert tracker.projection.configured_program.paths == ("src/helper.py", "src/main.py")


def test_projection_refresh_advances_only_the_changed_generation_surface() -> None:
    tracker = ScopeGenerationTracker(_projection())
    trust_only = _projection(trust=("new.py", "omitted/manual.py", "src/helper.py", "src/main.py"))
    after_trust = tracker.update_projection(trust_only)
    assert (after_trust.trust_inventory, after_trust.configured_program) == (1, 1)

    program_change = _projection(
        trust=("new.py", "omitted/manual.py", "src/helper.py", "src/main.py"),
        program=("new.py", "src/helper.py", "src/main.py"),
    )
    after_program = tracker.update_projection(program_change)
    assert (after_program.trust_inventory, after_program.configured_program) == (1, 2)


def test_stale_program_observation_returns_typed_not_ready_with_retry_metadata() -> None:
    tracker = ScopeGenerationTracker(_projection(), retry_after_seconds=0.25)
    assert tracker.observe_configured_program(1)
    tracker.apply_did_change_watched_files([WatchedFileEvent("src/main.py", FileChangeType.DELETED)])
    assert not tracker.observe_configured_program(1)

    result = tracker.wait_for_configured_program(0)

    assert result.code is ReadinessCode.NOT_READY
    assert not result.ready
    assert (result.target_generation, result.observed_generation) == (2, 1)
    assert result.retry is not None
    assert result.retry.retryable
    assert result.retry.retry_after_seconds == 0.25
    assert result.retry.target_generation == 2


def test_bounded_wait_unblocks_when_changed_generation_is_observed() -> None:
    tracker = ScopeGenerationTracker(_projection(), max_wait_seconds=0.5)
    tracker.apply_did_change_watched_files([WatchedFileEvent("src/main.py", FileChangeType.CHANGED)])

    observer = threading.Thread(target=lambda: (time.sleep(0.02), tracker.observe_configured_program(2)))
    observer.start()
    result = tracker.wait_for_configured_program(0.4)
    observer.join()

    assert result.code is ReadinessCode.READY
    assert result.observed_generation == 2


def test_timeout_is_clamped_to_barrier_bound_and_never_returns_empty_success() -> None:
    tracker = ScopeGenerationTracker(_projection(), max_wait_seconds=0.02)
    started = time.monotonic()

    result = tracker.wait_for_configured_program(10.0)

    elapsed = time.monotonic() - started
    assert result.code is ReadinessCode.NOT_READY
    assert result.retry is not None
    assert result.retry.timeout_seconds == 0.02
    assert 0.01 <= elapsed < 0.2


def test_clock_and_wait_are_injectable_for_deterministic_barrier_checks() -> None:
    now = [4.0]

    def advance_clock(_condition: threading.Condition, seconds: float) -> None:
        now[0] += seconds

    tracker = ScopeGenerationTracker(
        _projection(),
        max_wait_seconds=2.0,
        clock=lambda: now[0],
        waiter=advance_clock,
    )

    result = tracker.wait_for_configured_program(1.5)

    assert result.code is ReadinessCode.NOT_READY
    assert result.retry is not None
    assert result.retry.waited_seconds == 1.5
    assert now[0] == 5.5


def test_incompatible_projection_fails_readiness_without_waiting() -> None:
    projection = _projection(program=("ignored.py", "src/main.py"))
    tracker = ScopeGenerationTracker(projection, max_wait_seconds=1.0)
    started = time.monotonic()

    result = tracker.wait_for_configured_program()

    assert result.code is ReadinessCode.SCOPE_INCOMPATIBLE
    assert result.scope_error == projection.error
    assert result.retry is None
    assert time.monotonic() - started < 0.1


def test_watched_event_lsp_shape_uses_wire_change_type() -> None:
    event = WatchedFileEvent("src/main.py", FileChangeType.CREATED)
    assert event.as_lsp_change("file:///data/project") == {
        "uri": "file:///data/project/src/main.py",
        "type": 1,
    }
