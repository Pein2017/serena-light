"""Production-shaped Git-root evidence that the zero-write instrument sees everything."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.backend_eval.manifests import (
    EXCLUDED_DIRECTORY_NAMES,
    ManifestError,
    RootManifestRequest,
    capture_root_manifest,
    default_corpus_requests,
)
from scripts.backend_eval.write_guard import (
    WriteGuardError,
    assert_no_unexpected_writes,
    compare_root_manifests,
    enrich_after_manifest,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    """A production-shaped Git root: tracked source, ignored cache, empty dir, symlink."""

    root = tmp_path / "corpus"
    (root / "src").mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test User")
    (root / ".gitignore").write_text("model_cache/\n", encoding="utf-8")
    (root / "src" / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "README.md").write_text("corpus\n", encoding="utf-8")
    (root / "model_cache").mkdir()
    (root / "model_cache" / "weights.bin").write_bytes(b"weights")
    (root / "model_cache" / "empty").mkdir()
    (root / "link").symlink_to("README.md")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "noise.txt").write_text("noise\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "src/owner.py", "README.md")
    _git(root, "commit", "-m", "corpus")
    return root


def _request(root: Path) -> RootManifestRequest:
    return RootManifestRequest(
        root=root, kind="git", fully_hashed_paths=(), metadata_roots=(), required_config_paths=()
    )


def _capture(root: Path) -> object:
    return capture_root_manifest(_request(root))


def _delta_for(root: Path, mutate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    before = capture_root_manifest(_request(root))
    mutate()
    after = enrich_after_manifest(before, capture_root_manifest(_request(root)))
    delta = compare_root_manifests(before, after)
    return delta.unexpected, delta.control_changes


# --- the in-scope remainder ----------------------------------------------------------


def test_git_manifest_scans_the_complete_in_scope_remainder(tmp_path: Path) -> None:
    manifest = capture_root_manifest(_request(_repository(tmp_path)))
    hashed = {record.path for record in manifest.hashed_paths}
    remainder = {record.path: record for record in manifest.metadata_paths}
    assert hashed == {"src/owner.py"}
    assert manifest.inventory_paths == ("src/owner.py",)
    # Directories, files, symlinks, and the ignored cache tree are all in scope.
    assert "src" in remainder and remainder["src"].kind == "directory"
    assert "model_cache/empty" in remainder and remainder["model_cache/empty"].kind == "directory"
    assert remainder["model_cache/weights.bin"].kind == "file"
    assert remainder["link"].kind == "symlink"
    assert remainder["link"].symlink_target == "README.md"
    assert remainder["README.md"].disposition == "tracked"
    assert remainder["model_cache/weights.bin"].disposition == "ignored"
    for record in manifest.metadata_paths:
        assert record.content_sha256 is None


def test_git_manifest_excludes_only_the_declared_service_owned_trees(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (root / ".admission-artifacts").mkdir()
    (root / ".admission-artifacts" / "receipt.json").write_text("{}\n", encoding="utf-8")
    manifest = capture_root_manifest(_request(root))
    assert manifest.excluded_paths == (".admission-artifacts", ".git", ".venv", "node_modules")
    assert set(EXCLUDED_DIRECTORY_NAMES) == {".admission-artifacts", ".git", ".venv", "node_modules"}
    observed = {record.path for record in (*manifest.hashed_paths, *manifest.metadata_paths)}
    assert not any(path.startswith((".git/", ".venv/", "node_modules/", ".admission-artifacts/")) for path in observed)
    assert manifest.observed_count == manifest.in_scope_count + manifest.excluded_count
    assert manifest.excluded_count == 4


def test_a_trust_inventory_member_inside_a_pruned_tree_is_still_fully_hashed(tmp_path: Path) -> None:
    """Pruning bounds the *remainder* scan; the declared corpus closure is always hashed."""

    root = _repository(tmp_path)
    (root / "node_modules" / "vendored.js").write_text("export const A = 1;\n", encoding="utf-8")
    manifest = capture_root_manifest(_request(root))
    hashed = {record.path: record for record in manifest.hashed_paths}
    assert "node_modules/vendored.js" in hashed
    assert hashed["node_modules/vendored.js"].content_sha256 is not None
    assert not any(record.path.startswith("node_modules/") for record in manifest.metadata_paths)


def test_research_probes_model_cache_stays_in_scope_by_default() -> None:
    requests = {str(request.root): request for request in default_corpus_requests()}
    probes = requests["/data/CoordExp/.worktrees/research-probes"]
    assert probes.kind == "git"
    # The remainder scan owns model_cache now; no root may declare a second bounded scan.
    assert probes.metadata_roots == ()


def test_git_request_refuses_a_declared_metadata_root(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="metadata_roots"):
        RootManifestRequest(
            root=_repository(tmp_path),
            kind="git",
            fully_hashed_paths=(),
            metadata_roots=("model_cache",),
            required_config_paths=(),
        )


# --- every mutation class is an unexpected path and a hold ----------------------------


@pytest.mark.parametrize(
    ("name", "relative"),
    [
        ("new_module", "src/new_module.py"),
        ("pyrefly_config", "pyrefly.toml"),
        ("ty_config", "ty.toml"),
        ("pyright_config", "pyrightconfig.json"),
    ],
)
def test_a_new_workspace_file_is_an_unexpected_write(tmp_path: Path, name: str, relative: str) -> None:
    del name
    root = _repository(tmp_path)
    unexpected, _controls = _delta_for(root, lambda: (root / relative).write_text("x = 1\n", encoding="utf-8"))
    assert relative in unexpected


def test_a_new_cache_tree_is_an_unexpected_write(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    def _mutate() -> None:
        (root / ".pyrefly_cache").mkdir()
        (root / ".pyrefly_cache" / "x").write_text("cache\n", encoding="utf-8")

    unexpected, _controls = _delta_for(root, _mutate)
    assert ".pyrefly_cache" in unexpected
    assert ".pyrefly_cache/x" in unexpected


def test_a_new_empty_directory_is_an_unexpected_write(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    unexpected, _controls = _delta_for(root, lambda: (root / "empty_new").mkdir())
    assert "empty_new" in unexpected


def test_a_deleted_remainder_path_is_an_unexpected_write(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    unexpected, _controls = _delta_for(root, lambda: (root / "model_cache" / "weights.bin").unlink())
    assert "model_cache/weights.bin" in unexpected


def test_an_inventory_change_is_a_write_delta_not_an_unstable_root(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = capture_root_manifest(_request(root))
    (root / "src" / "added.py").write_text("A = 1\n", encoding="utf-8")
    after = enrich_after_manifest(before, capture_root_manifest(_request(root)))
    delta = compare_root_manifests(before, after)
    assert "src/added.py" in delta.unexpected
    assert delta.control_changes == ("inventory_count", "inventory_digest", "inventory_paths")
    with pytest.raises(WriteGuardError, match="unexpected"):
        assert_no_unexpected_writes((delta,))


def test_a_control_change_alone_is_reported_and_held(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = capture_root_manifest(_request(root))
    _git(root, "commit", "--allow-empty", "-m", "second")
    after = enrich_after_manifest(before, capture_root_manifest(_request(root)))
    delta = compare_root_manifests(before, after)
    assert delta.control_changes == ("source_revision",)
    with pytest.raises(WriteGuardError, match="control_changes"):
        assert_no_unexpected_writes((delta,))


# --- the two-stage remainder algorithm --------------------------------------------------


def test_enrichment_hashes_only_changed_or_created_remainder_files(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = capture_root_manifest(_request(root))
    (root / "model_cache" / "weights.bin").write_bytes(b"weights-changed")
    (root / "model_cache" / "fresh.bin").write_bytes(b"fresh")
    after = enrich_after_manifest(before, capture_root_manifest(_request(root)))
    hashed_after = {
        record.path: record.content_sha256 for record in after.metadata_paths if record.content_sha256 is not None
    }
    assert set(hashed_after) == {"model_cache/weights.bin", "model_cache/fresh.bin"}
    assert after.manifest_digest != capture_root_manifest(_request(root)).manifest_digest
    delta = compare_root_manifests(before, after)
    assert delta.after_manifest_digest == after.manifest_digest
    assert set(delta.unexpected) >= {"model_cache/weights.bin", "model_cache/fresh.bin"}


def test_enrichment_does_not_hash_deleted_directories_or_symlinks(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = capture_root_manifest(_request(root))
    (root / "link").unlink()
    (root / "link").symlink_to("src/owner.py")
    (root / "model_cache" / "empty").rmdir()
    (root / "later").mkdir()
    after = enrich_after_manifest(before, capture_root_manifest(_request(root)))
    by_path = {record.path: record for record in after.metadata_paths}
    assert by_path["link"].content_sha256 is None
    assert by_path["later"].content_sha256 is None
    delta = compare_root_manifests(before, after)
    assert set(delta.unexpected) >= {"link", "later", "model_cache/empty"}


def test_a_race_during_enrichment_is_incomplete_never_clean(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = capture_root_manifest(_request(root))
    (root / "model_cache" / "weights.bin").write_bytes(b"changed")
    after = capture_root_manifest(_request(root))
    target = root / "model_cache" / "weights.bin"

    def _racing_digest(path: Path) -> str | None:
        if path == target:
            # Another writer lands between the metadata capture and the content read.
            path.write_bytes(b"raced-again-and-longer")
            return None
        return None

    with pytest.raises(WriteGuardError, match="could not be hashed"):
        enrich_after_manifest(before, after, digest_for=_racing_digest)


def test_a_metadata_change_during_enrichment_is_incomplete(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = capture_root_manifest(_request(root))
    target = root / "model_cache" / "weights.bin"
    target.write_bytes(b"changed")
    after = capture_root_manifest(_request(root))
    target.write_bytes(b"changed-again-with-a-different-length")
    with pytest.raises(WriteGuardError, match="changed while it was being hashed"):
        enrich_after_manifest(before, after)


# --- individual freeze races stay fail-closed --------------------------------------------


def test_a_mutation_during_one_capture_fails_that_capture_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.backend_eval.manifests as manifests

    root = _repository(tmp_path)
    original = manifests._scan_remainder

    def _mutating_scan(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        (root / "src" / "raced.py").write_text("R = 1\n", encoding="utf-8")
        return result

    monkeypatch.setattr(manifests, "_scan_remainder", _mutating_scan)
    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_request(root))


def test_capture_stops_cooperatively_at_the_deadline(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    calls = {"n": 0}

    def _check() -> None:
        calls["n"] += 1
        if calls["n"] > 3:
            raise TimeoutError("deadline")

    with pytest.raises(TimeoutError):
        capture_root_manifest(_request(root), check=_check)


def test_scan_refuses_a_root_that_is_a_symlink(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    link = tmp_path / "linked-corpus"
    link.symlink_to(root)
    with pytest.raises(ManifestError, match="symlink"):
        capture_root_manifest(
            RootManifestRequest(
                root=link, kind="git", fully_hashed_paths=(), metadata_roots=(), required_config_paths=()
            )
        )


def test_remainder_records_are_disjoint_from_hashed_records(tmp_path: Path) -> None:
    manifest = capture_root_manifest(_request(_repository(tmp_path)))
    hashed = {record.path for record in manifest.hashed_paths}
    remainder = {record.path for record in manifest.metadata_paths}
    assert hashed & remainder == set()
    assert os.path.isabs(manifest.root)
