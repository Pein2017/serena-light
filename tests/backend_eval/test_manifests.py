"""Focused, disposable-fixture tests for evaluation corpus manifests."""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import SkipTest

import pytest

import serena_light.workspace.inventory as inventory_module
from scripts.backend_eval.manifests import (
    DIGEST_CHUNK_SIZE,
    LLM_FRAMEWORK_STUDY_SITE_PACKAGES,
    MS_TRANSFORMERS_ROOT,
    ManifestError,
    RootManifestRequest,
    bounded_file_digests,
    capture_root_manifest,
    default_corpus_requests,
)
from scripts.backend_eval.models import PathRecord
from scripts.backend_eval.process import CommandBytesResult, Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.production_helper import (
    ProductionHelperError,
    ProductionHelperTimeout,
    run_production_helper,
)
from scripts.backend_eval.source_binding import HelperExpectation
from serena_light.workspace.inventory import git_trust_inventory, observe_file_digest
from tests.backend_eval.support import real_expectation


class _FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_git_child_uses_only_exact_root_trust_and_config_free_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.backend_eval.manifests as manifests

    root = _repository(tmp_path)
    observed: dict[str, object] = {}

    def capture(
        command: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float | None,
        pass_fds: Sequence[int],
    ) -> CommandBytesResult:
        observed.update(
            command=tuple(command), cwd=cwd, env=dict(env), timeout=timeout,
            pass_fds=tuple(pass_fds), config=os.pread(pass_fds[0], 4096, 0),
            global_config=env["GIT_CONFIG_GLOBAL"],
        )
        return CommandBytesResult(0, b"ok\n", b"")

    monkeypatch.setattr(manifests, "run_bounded_bytes", capture)
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/root/.gitconfig")
    assert manifests._git_bytes(root, ("rev-parse", "HEAD"), None) == b"ok\n"
    assert observed["command"] == (
        "/usr/bin/git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"
    )
    assert observed["cwd"] == root
    assert observed["env"] == {
        "GIT_CONFIG_GLOBAL": observed["global_config"],
        "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin",
    }
    assert str(observed["global_config"]).startswith("/proc/")
    assert observed["config"] == f'[safe]\n\tdirectory = "{root}"\n'.encode()
    assert len(observed["pass_fds"]) == 1  # type: ignore[arg-type]


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mismatch creation")
def test_exact_dubious_ownership_worktree_is_accepted_without_user_config(tmp_path: Path) -> None:
    import scripts.backend_eval.manifests as manifests

    repository = _repository(tmp_path)
    (repository / "tracked.py").write_text("tracked = True\n", encoding="utf-8")
    _git(repository, "add", "tracked.py")
    _git(repository, "commit", "-m", "fixture")
    worktree = tmp_path / "owned-by-another-user"
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    try:
        os.chown(worktree, 1000, 1000)
    except (OSError, PermissionError) as error:
        raise SkipTest(f"cannot create a dubious-ownership worktree: {error}") from error
    assert manifests._git_bytes(worktree, ("rev-parse", "--show-toplevel"), None).strip() == str(
        worktree
    ).encode()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test User")
    return root


def _git_request(root: Path, **overrides: object) -> RootManifestRequest:
    fields: dict[str, object] = {
        "root": root,
        "kind": "git",
        "fully_hashed_paths": (),
        "metadata_roots": (),
        "required_config_paths": (),
    }
    fields.update(overrides)
    return RootManifestRequest(**fields)


def _non_git_request(root: Path, **overrides: object) -> RootManifestRequest:
    fields: dict[str, object] = {
        "root": root,
        "kind": "non_git",
        "fully_hashed_paths": (),
        "metadata_roots": (),
        "required_config_paths": (),
    }
    fields.update(overrides)
    return RootManifestRequest(**fields)


def _mutate_after_metadata_records(
    monkeypatch: pytest.MonkeyPatch, mutation: Callable[[], None]
) -> Callable[[], bool]:
    """Inject one controlled write after all records exist but before a final Git snapshot."""

    import scripts.backend_eval.manifests as manifests

    original = manifests._scan_remainder
    mutated = False

    def capture_then_mutate(*args: object, **kwargs: object) -> tuple[tuple[PathRecord, ...], tuple[str, ...]]:
        nonlocal mutated
        records = original(*args, **kwargs)  # type: ignore[arg-type]
        mutation()
        mutated = True
        return records

    monkeypatch.setattr(manifests, "_scan_remainder", capture_then_mutate)
    return lambda: mutated


def test_git_manifest_hashes_the_closure_and_metadata_scans_the_whole_remainder(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".gitignore").write_text("model_cache/\nother_cache/\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("answer = 42\n", encoding="utf-8")
    (root / "pyrightconfig.json").write_text('{"include": ["src"]}\n', encoding="utf-8")
    (root / "model_cache").mkdir()
    (root / "model_cache" / "blob.bin").write_bytes(b"cache")
    (root / "other_cache").mkdir()
    (root / "other_cache" / "must-not-be-scanned.bin").write_bytes(b"other")
    _git(root, "add", ".gitignore", "src/a.py")
    _git(root, "commit", "-m", "fixture")

    request = _git_request(root, required_config_paths=("pyrightconfig.json",))
    manifest = capture_root_manifest(request, expectation=real_expectation())

    assert {record.path for record in manifest.hashed_paths} == {"src/a.py", "pyrightconfig.json"}
    # Every remaining in-scope path is captured, including both ignored cache trees.
    assert {record.path for record in manifest.metadata_paths} == {
        ".gitignore",
        "src",
        "model_cache",
        "model_cache/blob.bin",
        "other_cache",
        "other_cache/must-not-be-scanned.bin",
    }
    assert manifest.excluded_paths == (".git",)
    assert manifest.inventory_digest == git_trust_inventory(root).digest
    assert manifest.inventory_count == 1
    assert manifest.inventory_paths == ("src/a.py",)
    assert {record.path: record.disposition for record in manifest.hashed_paths} == {
        "pyrightconfig.json": "untracked",
        "src/a.py": "tracked",
    }
    remainder = {record.path: record.disposition for record in manifest.metadata_paths}
    assert remainder["model_cache/blob.bin"] == "ignored"
    assert remainder[".gitignore"] == "tracked"
    assert remainder["src"] == "tracked"


def test_git_manifest_records_head_and_untracked_inventory_disposition(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "tracked.py").write_text("tracked = True\n", encoding="utf-8")
    _git(root, "add", "src/tracked.py")
    _git(root, "commit", "-m", "fixture")
    (root / "src" / "untracked.py").write_text("untracked = True\n", encoding="utf-8")

    manifest = capture_root_manifest(_git_request(root), expectation=real_expectation())

    assert manifest.source_revision == _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    assert {record.path: record.disposition for record in manifest.hashed_paths} == {
        "src/tracked.py": "tracked",
        "src/untracked.py": "untracked",
    }


def test_manifest_is_byte_stable_and_lexically_ordered(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "z.py").write_text("z = 1\n", encoding="utf-8")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "pyrightconfig.json").write_text("{}\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    request = _git_request(root, required_config_paths=("pyrightconfig.json",))

    first = capture_root_manifest(request, expectation=real_expectation())
    second = capture_root_manifest(request, expectation=real_expectation())

    assert first == second
    assert first.manifest_digest == second.manifest_digest
    assert tuple(record.path for record in first.hashed_paths) == ("a.py", "pyrightconfig.json", "z.py")


def test_manifest_does_not_follow_symlinked_directory(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"outside")
    (root / "model_cache").symlink_to(outside, target_is_directory=True)

    manifest = capture_root_manifest(_git_request(root), expectation=real_expectation())

    record = next(item for item in manifest.metadata_paths if item.path == "model_cache")
    assert (record.kind, record.symlink_target) == ("symlink", str(outside))
    assert not any(item.path.startswith("model_cache/") for item in manifest.metadata_paths)


def test_metadata_leaf_symlink_is_recorded_without_reading_its_target(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    (root / "model_cache").mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "model_cache" / "link.bin").symlink_to(outside)

    manifest = capture_root_manifest(_git_request(root), expectation=real_expectation())

    record = next(item for item in manifest.metadata_paths if item.path == "model_cache/link.bin")
    assert (record.kind, record.symlink_target, record.content_sha256) == ("symlink", str(outside), None)
    assert any(item.path == "model_cache" and item.kind == "directory" for item in manifest.metadata_paths)


def test_remainder_scan_refuses_a_directory_swapped_to_a_symlink_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory replaced by a symlink between its ``lstat`` and its ``open`` fails closed.

    The scan never follows the substituted link out of the root; it stops instead.
    """

    root = _repository(tmp_path)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    nested = root / "model_cache" / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.bin").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-not-be-enumerated.bin").write_bytes(b"outside")

    import scripts.backend_eval.manifests as manifests

    original = manifests._metadata_record
    substituted = False

    def racing_record(relative: str, kind: str, *args: object, **kwargs: object) -> PathRecord:
        nonlocal substituted
        record = original(relative, kind, *args, **kwargs)  # type: ignore[arg-type]
        if relative == "model_cache/nested" and kind == "directory" and not substituted:
            substituted = True
            nested.rename(root / "model_cache" / "nested-old")
            nested.symlink_to(outside, target_is_directory=True)
        return record

    monkeypatch.setattr(manifests, "_metadata_record", racing_record)

    with pytest.raises(ManifestError, match="cannot open corpus directory"):
        capture_root_manifest(_git_request(root), expectation=real_expectation())
    assert substituted


def test_production_rejects_a_same_size_rewrite_while_it_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production's own two-pass observation still refuses a file rewritten mid-hash.

    The digest itself now runs in a bounded child, so this pins the production semantics the
    child executes rather than the evaluator's bracket around it.
    """

    source = tmp_path / "module.py"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    real_read = inventory_module.os.read
    rewritten = False

    def racing_read(file_descriptor: int, size: int) -> bytes:
        nonlocal rewritten
        chunk = real_read(file_descriptor, size)
        if chunk and not rewritten and os.fstat(file_descriptor).st_ino == source.stat().st_ino:
            rewritten = True
            with source.open("r+b") as stream:
                stream.write(b"y" * (1024 * 1024))
                stream.flush()
                os.fsync(stream.fileno())
        return chunk

    monkeypatch.setattr(inventory_module.os, "read", racing_read)

    assert observe_file_digest(source) is None
    assert rewritten


def test_manifest_rejects_a_hashed_path_that_moves_around_the_digest_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evaluator's own bracket: a hashed path that moves while the child runs is unstable.

    ``_hashed_records`` ``lstat``s every path before the bounded digest chunk and again after
    it, so a rewrite anywhere inside that window -- including one production's own two-pass
    check happened to miss -- can never be recorded as a clean content hash.
    """

    import scripts.backend_eval.manifests as manifests

    root = _repository(tmp_path)
    source = root / "module.py"
    source.write_bytes(b"x" * 1024)
    _git(root, "add", "module.py")
    _git(root, "commit", "-m", "fixture")
    real_digests = manifests.bounded_file_digests
    rewritten = False

    def racing_digests(
        digest_root: Path,
        relatives: Sequence[str],
        *,
        expectation: HelperExpectation,
        deadline: Deadline | None,
    ) -> dict[str, str | None]:
        nonlocal rewritten
        result = real_digests(digest_root, relatives, expectation=expectation, deadline=deadline)
        if not rewritten and "module.py" in result:
            rewritten = True
            source.write_bytes(b"y" * 1024)
        return result

    monkeypatch.setattr(manifests, "bounded_file_digests", racing_digests)

    with pytest.raises(ManifestError, match="unstable"):
        capture_root_manifest(_git_request(root), expectation=real_expectation())
    assert rewritten


def test_git_manifest_rejects_untracked_source_created_after_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    _git(root, "add", "source.py")
    _git(root, "commit", "-m", "fixture")
    was_mutated = _mutate_after_metadata_records(
        monkeypatch,
        lambda: (root / "created.py").write_text("created = True\n", encoding="utf-8"),
    )

    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_git_request(root), expectation=real_expectation())
    assert was_mutated()


def test_git_manifest_rejects_untracked_source_deleted_after_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    _git(root, "add", "source.py")
    _git(root, "commit", "-m", "fixture")
    untracked = root / "untracked.py"
    untracked.write_text("untracked = True\n", encoding="utf-8")
    was_mutated = _mutate_after_metadata_records(monkeypatch, untracked.unlink)

    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_git_request(root), expectation=real_expectation())
    assert was_mutated()


def test_git_manifest_rejects_head_changed_after_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    readme = root / "README.md"
    readme.write_text("before\n", encoding="utf-8")
    _git(root, "add", "source.py", "README.md")
    _git(root, "commit", "-m", "fixture")

    def replace_head() -> None:
        readme.write_text("after\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "changed-head")

    was_mutated = _mutate_after_metadata_records(monkeypatch, replace_head)

    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_git_request(root), expectation=real_expectation())
    assert was_mutated()


def test_non_git_manifest_hashes_only_declared_task_paths_without_inventory_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "site-packages"
    selected = root / "torchtune" / "__init__.py"
    selected.parent.mkdir(parents=True)
    selected.write_text("selected = True\n", encoding="utf-8")
    unselected = root / "other-package" / "large.py"
    unselected.parent.mkdir()
    unselected.write_text("must_not_be_read = True\n", encoding="utf-8")

    import scripts.backend_eval.manifests as manifests

    def forbidden_inventory(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("exact non-Git task paths must not walk the full environment")

    monkeypatch.setattr(manifests, "_bounded_non_git_inventory", forbidden_inventory)
    manifest = capture_root_manifest(
                   _non_git_request(root, fully_hashed_paths=("torchtune/__init__.py",)),
                   expectation=real_expectation(),
               )

    assert manifest.kind == "non_git"
    assert manifest.source_revision is None
    assert manifest.inventory_count == 1
    assert tuple(record.path for record in manifest.hashed_paths) == ("torchtune/__init__.py",)
    assert manifest.hashed_paths[0].disposition == "declared"


def test_manifest_rejects_missing_roots_special_files_duplicate_paths_and_traversal(tmp_path: Path) -> None:
    missing = _non_git_request(tmp_path / "missing", fully_hashed_paths=("needed.py",))
    with pytest.raises(ManifestError, match="missing"):
        capture_root_manifest(missing, expectation=real_expectation())

    root = _repository(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    fifo = root / "pyrightconfig.json"
    os.mkfifo(fifo)
    _git(root, "add", "src/a.py")
    _git(root, "commit", "-m", "fixture")

    with pytest.raises(ManifestError, match="regular"):
        capture_root_manifest(
            _git_request(root, required_config_paths=("pyrightconfig.json",)),
            expectation=real_expectation(),
        )
    with pytest.raises(ManifestError, match="duplicate"):
        capture_root_manifest(
            _git_request(root, fully_hashed_paths=("src/a.py", "src/a.py")),
            expectation=real_expectation(),
        )
    with pytest.raises(ManifestError, match="traversal"):
        capture_root_manifest(_git_request(root, fully_hashed_paths=("../outside.py",)), expectation=real_expectation())


def test_default_corpus_requests_are_fixed_and_do_not_capture_live_roots() -> None:
    requests = default_corpus_requests()
    by_root = {request.root: request for request in requests}

    assert tuple(request.root for request in requests) == tuple(sorted(by_root))
    assert set(by_root) == {
        Path("/data/CoordExp/serena-light"),
        Path("/data/CoordExp/.worktrees/research-probes"),
        Path("/data/ms-swift"),
        MS_TRANSFORMERS_ROOT,
        LLM_FRAMEWORK_STUDY_SITE_PACKAGES,
    }
    ms_swift_request = by_root[Path("/data/ms-swift")]
    assert ms_swift_request.required_config_paths == ("setup.cfg",)
    assert (ms_swift_request.root / ms_swift_request.required_config_paths[0]).is_file()
    assert by_root[Path("/data/CoordExp/.worktrees/research-probes")].metadata_roots == ()
    assert by_root[LLM_FRAMEWORK_STUDY_SITE_PACKAGES].fully_hashed_paths == (
        "torchtune/__init__.py",
        "torchtune/config/__init__.py",
        "torchtune/config/_parse.py",
    )


# --- the bounded production-helper digest child -----------------------------------------


def test_bounded_digests_equal_the_production_helper(tmp_path: Path) -> None:
    """The batched child returns exactly what ``observe_file_digest`` returns per path."""

    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    relatives = []
    for index in range(7):
        relative = f"pkg/module_{index}.py"
        (root / relative).write_text(f"value = {index}\n", encoding="utf-8")
        relatives.append(relative)

    observed = bounded_file_digests(root, tuple(relatives), deadline=None, expectation=real_expectation())

    assert observed == {relative: observe_file_digest(root / relative) for relative in relatives}
    assert all(digest is not None for digest in observed.values())


def test_bounded_digests_chunk_a_large_batch(tmp_path: Path) -> None:
    """A batch larger than one chunk is still complete, ordered, and correct."""

    root = tmp_path / "tree"
    root.mkdir()
    relatives = tuple(f"file_{index:04d}.py" for index in range(DIGEST_CHUNK_SIZE + 5))
    for relative in relatives:
        (root / relative).write_text(f"# {relative}\n", encoding="utf-8")

    observed = bounded_file_digests(root, relatives, deadline=None, expectation=real_expectation())

    assert sorted(observed) == sorted(relatives)
    assert observed[relatives[-1]] == observe_file_digest(root / relatives[-1])


def test_bounded_digests_report_a_fifo_as_unattributable(tmp_path: Path) -> None:
    """A blocking special node yields ``None`` promptly, exactly as production does."""

    root = tmp_path / "tree"
    root.mkdir()
    os.mkfifo(root / "blocked.py")

    started = time.monotonic()
    observed = bounded_file_digests(root, ("blocked.py",), deadline=None, expectation=real_expectation())
    elapsed = time.monotonic() - started

    assert observed == {"blocked.py": None}
    assert elapsed < 10.0


def test_a_hashed_path_that_cannot_be_attributed_is_unstable(tmp_path: Path) -> None:
    """A ``None`` digest is a fail-closed manifest error, never a clean record."""

    root = tmp_path / "fixture"
    root.mkdir()
    (root / "a.py").write_text("answer = 1\n", encoding="utf-8")
    request = RootManifestRequest(
        root=root,
        kind="non_git",
        fully_hashed_paths=("a.py",),
        metadata_roots=(),
        required_config_paths=(),
    )
    (root / "a.py").unlink()
    os.mkfifo(root / "a.py")

    started = time.monotonic()
    with pytest.raises(ManifestError, match="not a regular file"):
        capture_root_manifest(request, expectation=real_expectation())
    assert time.monotonic() - started < 10.0


def test_a_corpus_capture_child_is_bounded_and_its_group_is_killed(tmp_path: Path) -> None:
    """A digest helper that blocks costs the phase its remaining time, not the whole run."""

    marker = tmp_path / "child.pid"
    stub = tmp_path / "hung-python"
    stub.write_text(
        f'#!/bin/sh\necho "$$" > "{marker}"\nwhile true; do sleep 1; done\n', encoding="utf-8"
    )
    stub.chmod(0o700)
    deadline = Deadline.start(monotonic_clock, 2.0)

    started = time.monotonic()
    with pytest.raises(ProductionHelperTimeout, match="process group was killed"):
        run_production_helper(
            "observe_file_digests",
            {"paths": []},
            expectation=real_expectation(),
            deadline=deadline,
            python=stub,
        )
    assert time.monotonic() - started < 20.0

    pid = int(marker.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_capture_with_no_remaining_time_fails_closed(tmp_path: Path) -> None:
    """A phase already at its ceiling never starts another digest child."""

    root = tmp_path / "fixture"
    root.mkdir()
    (root / "a.py").write_text("answer = 1\n", encoding="utf-8")
    request = RootManifestRequest(
        root=root,
        kind="non_git",
        fully_hashed_paths=("a.py",),
        metadata_roots=(),
        required_config_paths=(),
    )
    clock = _FakeClock()
    deadline = Deadline.start(clock, 10.0)
    clock.advance(10.0)

    with pytest.raises((ManifestError, DeadlineExceeded)):
        capture_root_manifest(request, deadline=deadline, expectation=real_expectation())


def test_a_forced_swap_during_a_capture_never_hangs(tmp_path: Path) -> None:
    """A path replaced by a FIFO while the digest child runs cannot block the phase.

    Production's own guarded read opens ``O_RDONLY | O_NOFOLLOW`` without ``O_NONBLOCK``, so
    a node substituted after its type was inspected blocks that open indefinitely.  Running
    production's exact bytes in a bounded child does not remove the race -- it bounds it: the
    capture either completes with a stable observation or fails typed inside the deadline.
    """

    root = tmp_path / "fixture"
    root.mkdir()
    relatives = tuple(f"file_{index:04d}.py" for index in range(1200))
    for relative in relatives:
        (root / relative).write_text(f"# {relative}\n", encoding="utf-8")
    request = RootManifestRequest(
        root=root,
        kind="non_git",
        fully_hashed_paths=relatives,
        metadata_roots=(),
        required_config_paths=(),
    )
    target = root / relatives[-1]
    stop = threading.Event()

    def swap() -> None:
        while not stop.is_set():
            try:
                target.unlink()
                os.mkfifo(target)
                target.unlink()
                target.write_text("# restored\n", encoding="utf-8")
            except OSError:
                return

    swapper = threading.Thread(target=swap, daemon=True)
    started = time.monotonic()
    swapper.start()
    try:
        with contextlib.suppress(ManifestError, ProductionHelperError, DeadlineExceeded):
            capture_root_manifest(
                request, deadline=Deadline.start(monotonic_clock, 15.0),
                expectation=real_expectation(),
            )
    finally:
        stop.set()
        swapper.join(timeout=5.0)

    assert time.monotonic() - started < 25.0
