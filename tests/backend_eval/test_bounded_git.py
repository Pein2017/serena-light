"""Every Git child of a corpus capture is bounded, and the inventory stays production's.

The evaluation may not modify `src/serena_light`, and
`serena_light.workspace.inventory.git_trust_inventory` starts its own unbounded
`git ls-files`.  Rather than leaving that one call outside the phase deadline, the
evaluation reads the same command through the bounded runner and hands those bytes to
production's *own* normalization and inspection helpers -- executed in the source-bound
sealed child, never in the evaluator process.  These tests own the three claims that makes:
the resulting inventory is field-for-field exactly production's, no evaluation path starts a
Git child any other way, and importing the evaluator leaves no production module behind in
the parent.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import scripts.backend_eval.manifests as manifests
import scripts.backend_eval.production_helper as production_helper
from scripts.backend_eval.manifests import (
    BoundedTrustInventory,
    RootManifestRequest,
    _bounded_non_git_inventory,
    _git_environment,
    _git_trust_inventory_from_bounded_bytes,
    capture_root_manifest,
)
from scripts.backend_eval.process import CommandBytesResult, Deadline, monotonic_clock, run_bounded_bytes
from tests.backend_eval.support import real_expectation

# The evaluator must not import production; a *test* comparing the child's answer with
# production's own must, so it does so here and nowhere in ``scripts/backend_eval``.
from serena_light.workspace.inventory import (  # isort: skip
    bounded_non_git_trust_inventory,
    git_trust_inventory,
)

COMBINED_LS_FILES = ("ls-files", "--cached", "--others", "--exclude-standard", "-z")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    """A root shaped like the real corpus: tracked, untracked, unsupported, and rejected."""

    root = tmp_path / "corpus"
    (root / "src" / "pkg").mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test User")
    (root / "src" / "pkg" / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "pkg" / "typed.pyi").write_text("VALUE: int\n", encoding="utf-8")
    (root / "src" / "bundle.ts").write_text("export const A = 1;\n", encoding="utf-8")
    (root / "README.md").write_text("not a supported extension\n", encoding="utf-8")
    (root / "with space.py").write_text("SPACED = 1\n", encoding="utf-8")
    (root / "unicode_é.py").write_text("UNICODE = 1\n", encoding="utf-8")
    _git(root, "add", "src/pkg/owner.py", "README.md")
    _git(root, "commit", "-m", "corpus")
    return root


def _rejecting_repository(tmp_path: Path) -> Path:
    """The same root plus candidates production's inspection rejects rather than accepts."""

    root = _repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = 1\n", encoding="utf-8")
    (root / "escape.py").symlink_to(outside)
    (root / "internal.py").symlink_to("src/pkg/owner.py")
    (root / "broken.py").symlink_to("src/pkg/absent.py")
    return root


def _request(root: Path) -> RootManifestRequest:
    return RootManifestRequest(
        root=root, kind="git", fully_hashed_paths=(), metadata_roots=(), required_config_paths=()
    )


def _bounded_inventory(root: Path) -> BoundedTrustInventory:
    return _git_trust_inventory_from_bounded_bytes(
        root, _bounded_combined(root), real_expectation(), None
    )


def _bounded_combined(root: Path) -> bytes:
    result = run_bounded_bytes(
        ["/usr/bin/git", *COMBINED_LS_FILES], cwd=root, env=_git_environment(), timeout=60.0
    )
    assert result.returncode == 0
    return result.stdout


# --- exact equality with production ------------------------------------------------


@pytest.mark.parametrize("fixture", ["plain", "rejecting"])
def test_the_bounded_inventory_equals_productions_in_every_field(tmp_path: Path, fixture: str) -> None:
    root = _repository(tmp_path) if fixture == "plain" else _rejecting_repository(tmp_path)

    expected = git_trust_inventory(root)
    observed = _bounded_inventory(root)

    assert observed.root == str(expected.root)
    assert observed.kind == expected.kind == "git"
    assert observed.paths == expected.paths
    assert observed.count == expected.count
    assert observed.digest == expected.digest
    assert observed.rejected == tuple(sorted((entry.path, entry.reason) for entry in expected.rejected))


def test_the_bounded_non_git_inventory_equals_productions_in_every_field(tmp_path: Path) -> None:
    """The other delegated helper, on a disposable fixture, field for field.

    ``bounded_non_git_trust_inventory`` used to run in the evaluator process.  It now runs in
    the sealed child, so this equality is what proves the move preserved production's exact
    answer -- the bounded walk's pruning, the extension filter, the symlink rejections, the
    root resolution, and the digest.
    """

    root = tmp_path / "site-packages"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("A = 1\n", encoding="utf-8")
    (root / "pkg" / "sub" / "mod.py").write_text("B = 2\n", encoding="utf-8")
    (root / "pkg" / "notes.md").write_text("unsupported extension\n", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "skipped.py").write_text("C = 3\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("D = 4\n", encoding="utf-8")
    (root / "pkg" / "escape.py").symlink_to(outside)

    expected = bounded_non_git_trust_inventory(root)
    observed = _bounded_non_git_inventory(root, real_expectation(), None)

    assert observed.root == str(expected.root)
    assert observed.kind == expected.kind == "bounded_no_symlink"
    assert observed.paths == expected.paths
    assert observed.count == expected.count
    assert observed.digest == expected.digest
    assert observed.rejected == tuple(sorted((entry.path, entry.reason) for entry in expected.rejected))
    assert "pkg/__init__.py" in observed.paths
    assert "pkg/sub/mod.py" in observed.paths
    assert "pkg/notes.md" not in observed.paths
    assert ".hidden/skipped.py" not in observed.paths


def test_the_rejecting_fixture_actually_exercises_rejections(tmp_path: Path) -> None:
    """Guard the test above: an equality that compared two empty tuples would prove nothing."""

    root = _rejecting_repository(tmp_path)
    inventory = _bounded_inventory(root)
    assert inventory.rejected, "the fixture must produce at least one rejected candidate"
    assert {path for path, _reason in inventory.rejected} == {"broken.py", "escape.py", "internal.py"}
    assert inventory.paths, "the fixture must also accept candidates"
    assert "src/pkg/owner.py" in inventory.paths
    # Unsupported extensions are filtered, never rejected.
    assert "README.md" not in inventory.paths
    assert "README.md" not in {path for path, _reason in inventory.rejected}


def test_unicode_and_spaced_candidates_survive_the_bounded_decode(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    inventory = _bounded_inventory(root)
    assert "with space.py" in inventory.paths
    assert "unicode_é.py" in inventory.paths
    assert inventory.digest == git_trust_inventory(root).digest


# --- the unbounded helper is never called -------------------------------------------


def test_no_evaluation_path_references_the_unbounded_inventory_helper() -> None:
    assert not hasattr(manifests, "git_trust_inventory")
    source = Path(manifests.__file__).read_text(encoding="utf-8")
    # Word-bounded: ``bounded_non_git_trust_inventory`` is a different, subprocess-free call.
    assert re.search(r"(?<![_A-Za-z])git_trust_inventory\s*\(", source) is None


def test_git_manifest_capture_succeeds_with_the_unbounded_helper_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture must produce the same paths, digest, and rejections with the helper poisoned."""

    root = _repository(tmp_path)
    expected = git_trust_inventory(root)

    import serena_light.workspace.inventory as inventory_module

    def _forbidden(_root: Path) -> None:
        raise AssertionError("the evaluation called the unbounded git_trust_inventory")

    monkeypatch.setattr(inventory_module, "git_trust_inventory", _forbidden)

    manifest = capture_root_manifest(_request(root), expectation=real_expectation())

    assert manifest.inventory_digest == expected.digest
    assert manifest.inventory_count == expected.count
    assert manifest.inventory_paths == tuple(sorted(expected.paths))
    assert {record.path for record in manifest.hashed_paths} == set(expected.paths)
    # A rejected candidate still stops the capture, exactly as before.
    (root / "escape.py").symlink_to(tmp_path / "absent-target.py")
    with pytest.raises(manifests.ManifestError, match="trust inventory rejected"):
        capture_root_manifest(_request(root), expectation=real_expectation())


# --- subprocess accounting ------------------------------------------------------------


def test_every_child_of_a_capture_comes_through_the_bounded_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every process a capture starts -- Git *and* the digest helper -- is bounded and killable."""

    root = _repository(tmp_path)
    deadline = Deadline.start(monotonic_clock, 1800, reserve=300)

    bounded: list[tuple[tuple[str, ...], float | None]] = []
    spawned: list[Sequence[str]] = []
    sessions: list[object] = []
    real_bounded = manifests.run_bounded_bytes
    real_helper_bounded = production_helper.run_bounded_bytes
    real_popen = subprocess.Popen

    def recording_bounded(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None = None,
        pass_fds: Sequence[int] = (),
    ) -> CommandBytesResult:
        bounded.append((tuple(command), timeout))
        return real_bounded(command, cwd=cwd, env=env, timeout=timeout, pass_fds=pass_fds)

    def recording_helper_bounded(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None = None,
        stdin: bytes | None = None,
        pass_fds: Sequence[int] = (),
    ) -> CommandBytesResult:
        bounded.append((tuple(command), timeout))
        return real_helper_bounded(
            command, cwd=cwd, env=env, timeout=timeout, stdin=stdin, pass_fds=pass_fds
        )

    def recording_popen(args: Sequence[str], *rest: object, **kwargs: object) -> subprocess.Popen[bytes]:
        spawned.append(args)
        sessions.append(kwargs.get("start_new_session"))
        return real_popen(args, *rest, **kwargs)  # type: ignore[arg-type]

    def forbidden_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("an evaluation capture used unbounded subprocess.run")

    monkeypatch.setattr(manifests, "run_bounded_bytes", recording_bounded)
    monkeypatch.setattr(production_helper, "run_bounded_bytes", recording_helper_bounded)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(subprocess, "run", forbidden_run)

    capture_root_manifest(_request(root), deadline=deadline, expectation=real_expectation())

    # One child per bounded call and no others: nothing spawned a process another way, and
    # every one of them got its own session so its whole group can be killed on expiry.
    assert len(spawned) == len(bounded) > 0
    assert [tuple(command) for command in spawned] == [command for command, _timeout in bounded]
    assert sessions == [True] * len(spawned)
    git_commands = [command for command, _timeout in bounded if command[0] == str(manifests.GIT_EXECUTABLE)]
    # One `--show-toplevel` guard, then four bounded Git children per freeze state, twice.
    git_trust = ("-c", f"safe.directory={root}")
    assert [command[1:] for command in git_commands] == [
        (*git_trust, "rev-parse", "--show-toplevel"),
        *2
        * [
            (*git_trust, "rev-parse", "HEAD"),
            (*git_trust, "ls-files", "--cached", "-z"),
            (*git_trust, "ls-files", "--others", "--exclude-standard", "-z"),
            (*git_trust, *COMBINED_LS_FILES),
        ],
    ]
    # Three bounded production-helper children, each executing the sealed program under -I:
    # one inventory child per freeze state, and one digest child for the hashed closure.
    helpers = [command for command, _timeout in bounded if command[0] != str(manifests.GIT_EXECUTABLE)]
    assert len(helpers) == 3
    for helper in helpers:
        assert helper[1:3] == ("-I", "-B")
        # The program is a sealed in-memory image addressed by descriptor, never a pathname.
        assert re.fullmatch(r"/proc/self/fd/\d+", helper[3])
        # The production source it imports is the second sealed image, also by descriptor.
        assert helper[-1].isdigit()
    # Every child, of either family, carries the phase's own remaining time.
    for _command, timeout in bounded:
        assert timeout is not None and 0 < timeout <= 1500


def test_a_capture_without_a_deadline_still_only_uses_the_bounded_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    calls: list[float | None] = []
    real_bounded = manifests.run_bounded_bytes

    def recording_bounded(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None = None,
        pass_fds: Sequence[int] = (),
    ) -> CommandBytesResult:
        calls.append(timeout)
        return real_bounded(command, cwd=cwd, env=env, timeout=timeout, pass_fds=pass_fds)

    monkeypatch.setattr(manifests, "run_bounded_bytes", recording_bounded)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unbounded run")))

    capture_root_manifest(_request(root), expectation=real_expectation())

    assert len(calls) == 9, "one root guard plus two freeze states of four bounded children"


def test_a_hung_git_child_stops_the_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repository(tmp_path)
    from scripts.backend_eval.process import CommandTimeout

    def hanging_bounded(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None = None,
        pass_fds: Sequence[int] = (),
    ) -> CommandBytesResult:
        del command, cwd, env, timeout, pass_fds
        raise CommandTimeout("git timed out after 1s and its process group was killed")

    monkeypatch.setattr(manifests, "run_bounded_bytes", hanging_bounded)

    with pytest.raises(manifests.ManifestError, match="timed out"):
        capture_root_manifest(_request(root), expectation=real_expectation())
