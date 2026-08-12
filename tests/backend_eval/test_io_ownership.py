"""The finite ownership table for every read and write the evaluator executes.

A safety review of this harness kept finding the same shape of defect in a different place:
one more path-based read or write that ``O_NOFOLLOW`` alone does not confine, or that a
substituted FIFO can block.  Grepping for ``os.open`` flags never found them all, because
``Path.read_bytes``, ``Path.write_text``, ``Path.mkdir`` and a production helper call are
each an unguarded open with no flag to grep for.

So the surface is enumerated structurally instead.  This test parses every module under
``scripts/backend_eval`` and collects *every* call that opens, creates, enumerates, reads, or
writes a filesystem object -- by descriptor, by pathname, or by delegation to a production
helper -- and requires the resulting set to equal the table below exactly.  A new read or
write anywhere in the evaluator fails this test until its owner is declared here, and a
removed one fails it until the row goes.  The table is the audit.

``docs/backend-eval-io-ownership.md`` is the prose companion: it explains what each owner
class guarantees and why the residual boundaries are the ones stated rather than closed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

EVALUATOR_ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_PACKAGE = EVALUATOR_ROOT / "scripts" / "backend_eval"
OWNERSHIP_DOC = EVALUATOR_ROOT / "docs" / "backend-eval-io-ownership.md"

# --- what counts as a filesystem access ------------------------------------------------
#
# Descriptor primitives, every pathname-shaped ``pathlib`` and builtin access, and every
# production helper that reads a file the evaluation does not own.  A helper that only
# decodes or normalizes strings is not filesystem access and is deliberately absent.

_MODULE_QUALIFIED = ("os", "shutil", "subprocess")
_ACCESS_NAMES = {
    "os.open": "os.open",
    "os.fdopen": "os.fdopen",
    "os.mkdir": "os.mkdir",
    "os.makedirs": "os.makedirs",
    "os.scandir": "os.scandir",
    "os.listdir": "os.listdir",
    "shutil.copy2": "shutil.copy2",
    "shutil.copytree": "shutil.copytree",
    "shutil.rmtree": "shutil.rmtree",
    "open": "open",
    "read_bytes": "Path.read_bytes",
    "read_text": "Path.read_text",
    "write_bytes": "Path.write_bytes",
    "write_text": "Path.write_text",
    "mkdir": "Path.mkdir",
    "makedirs": "Path.makedirs",
    "touch": "Path.touch",
    "iterdir": "Path.iterdir",
    "rglob": "Path.rglob",
    "glob": "Path.glob",
    "walk": "Path.walk",
    "observe_file_digest": "production.observe_file_digest",
    "dependency_lock_digest": "production.dependency_lock_digest",
    "compute_build_identity": "production.compute_build_identity",
    "runtime_paths": "production.runtime_paths",
    "bounded_non_git_trust_inventory": "production.bounded_non_git_trust_inventory",
    "git_trust_inventory": "production.git_trust_inventory",
    "open_guarded_directory": "production.open_guarded_directory",
}

# --- the owner classes -----------------------------------------------------------------

CONFINED = "confined"
"""Component-wise ``O_NOFOLLOW`` descriptor walk out from an already-proven open root."""

GUARDED = "guarded"
"""``O_NOFOLLOW | O_NONBLOCK`` plus an ``fstat`` regular-file proof on that same descriptor.

Used where no owning root descriptor exists: the caller's declared lock, an executable's
realpath outside every root, the evaluator's own source closure.
"""

DESCRIPTOR = "descriptor"
"""Operates on a descriptor this process already holds; there is no pathname to redirect."""

CHILD = "production-child"
"""Exact production semantics, executed in the bounded, killable, source-bound child."""

OWN_IMAGE = "own-image"
"""Reads this process's own sealed ``memfd`` through ``/proc/self/fd``; no attacker surface."""

# --- the table -------------------------------------------------------------------------
#
# (module, enclosing function, access, owner).  Line numbers are deliberately absent: they
# churn, and the audit question is who owns the access, not where it sits today.

OWNERSHIP: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        # admission.py -- the artifact tree, the receipt, and the publication lock.
        ("admission.py", "cleanup", "os.open", CONFINED),
        ("admission.py", "artifact_tree_digest", "os.open", CONFINED),
        ("admission.py", "_collect_artifact_entries", "os.scandir", DESCRIPTOR),
        ("admission.py", "_collect_artifact_entries", "os.open", CONFINED),
        ("admission.py", "_read_artifact_bytes", "os.open", CONFINED),
        ("admission.py", "_read_artifact_bytes", "os.fdopen", DESCRIPTOR),
        ("admission.py", "_publish_receipt", "os.open", CONFINED),
        ("admission.py", "_publication_lock", "os.open", CONFINED),
        ("admission.py", "_open_evaluation_directory", "os.open", CONFINED),
        ("admission.py", "_open_owned_child", "os.mkdir", CONFINED),
        ("admission.py", "_open_owned_child", "os.open", CONFINED),
        # candidate_lock.py -- the frozen lock transaction below the artifact root.
        ("candidate_lock.py", "_purge_quarantined_nodes", "os.scandir", DESCRIPTOR),
        ("candidate_lock.py", "_resolution_lock", "os.open", CONFINED),
        ("candidate_lock.py", "_artifact_directory", "os.open", CONFINED),
        ("candidate_lock.py", "_open_owned_directory", "os.mkdir", CONFINED),
        ("candidate_lock.py", "_open_owned_directory", "os.open", CONFINED),
        ("candidate_lock.py", "_open_regular_artifact", "os.open", CONFINED),
        ("candidate_lock.py", "_read_descriptor", "os.fdopen", DESCRIPTOR),
        ("candidate_lock.py", "_write_artifact", "os.open", CONFINED),
        ("candidate_lock.py", "_write_artifact", "os.fdopen", DESCRIPTOR),
        ("candidate_lock.py", "_purge_directory", "os.open", CONFINED),
        ("candidate_lock.py", "_purge_directory", "os.scandir", DESCRIPTOR),
        # identity.py -- the evaluator's own executed source closure.
        ("identity.py", "_source_closure", "os.scandir", DESCRIPTOR),
        ("identity.py", "_read_regular_file", "os.open", GUARDED),
        ("identity.py", "_read_regular_file", "os.fdopen", DESCRIPTOR),
        # manifests.py -- the corpus capture.
        ("manifests.py", "_capture_transformers_manifest", "production.bounded_non_git_trust_inventory", CHILD),
        ("manifests.py", "_scan_remainder", "os.open", CONFINED),
        ("manifests.py", "_walk_remainder", "os.scandir", DESCRIPTOR),
        ("manifests.py", "_walk_remainder", "os.open", CONFINED),
        ("manifests.py", "_walk_metadata_root", "production.open_guarded_directory", CONFINED),
        ("manifests.py", "_walk_metadata_root", "os.scandir", DESCRIPTOR),
        # production_child.py -- the bounded child itself.
        ("production_child.py", "_production_files", "Path.read_bytes", CHILD),
        ("production_child.py", "_production_identity", "production.runtime_paths", CHILD),
        ("production_child.py", "_production_identity", "production.compute_build_identity", CHILD),
        ("production_child.py", "_production_identity", "production.dependency_lock_digest", CHILD),
        ("production_child.py", "_observe_file_digests", "production.observe_file_digest", CHILD),
        # production_helper.py -- the sealed child program and the re-read of reported bytes.
        ("production_helper.py", "_open_owner_root", "os.open", GUARDED),
        ("production_helper.py", "_read_owned_file", "os.open", CONFINED),
        ("production_helper.py", "_read_owned_file", "os.fdopen", DESCRIPTOR),
        # production_identity.py -- the three declared lock inputs.
        ("production_identity.py", "_read_identity_inputs", "os.open", CONFINED),
        ("production_identity.py", "_read_guarded", "os.open", CONFINED),
        ("production_identity.py", "_read_guarded", "os.fdopen", DESCRIPTOR),
        # runtime.py -- the service-owned candidate runtime.
        ("runtime.py", "_runtime_lock", "os.open", CONFINED),
        ("runtime.py", "_open_confined_directory", "os.open", CONFINED),
        ("runtime.py", "_open_existing_confined_directory", "os.open", CONFINED),
        ("runtime.py", "_open_confined_child", "os.mkdir", CONFINED),
        ("runtime.py", "_open_confined_child", "os.open", CONFINED),
        ("runtime.py", "_scandir_names", "os.scandir", DESCRIPTOR),
        ("runtime.py", "_require_sealed_image", "Path.read_bytes", OWN_IMAGE),
        ("runtime.py", "_read_regular_file", "os.open", GUARDED),
        ("runtime.py", "_read_regular_file", "os.fdopen", DESCRIPTOR),
        ("runtime.py", "_open_confined_existing_child", "os.open", CONFINED),
        ("runtime.py", "_open_owned_write_leaf", "os.open", CONFINED),
        ("runtime.py", "_write_owned_descriptor", "os.fdopen", DESCRIPTOR),
        ("runtime.py", "_read_owned_descriptor", "os.fdopen", DESCRIPTOR),
        ("runtime.py", "_purge_directory_contents", "os.scandir", DESCRIPTOR),
        ("runtime.py", "_purge_directory_contents", "os.open", CONFINED),
        # source_binding.py -- the executed production helper closure.
        ("source_binding.py", "_read_regular_file", "os.open", GUARDED),
        ("source_binding.py", "_read_regular_file", "os.fdopen", DESCRIPTOR),
    }
)


class _AccessVisitor(ast.NodeVisitor):
    """Collect ``(enclosing function, access)`` for every filesystem call in one module."""

    def __init__(self) -> None:
        self.enclosing: list[str] = []
        self.found: set[tuple[str, str]] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.enclosing.append(node.name)
        self.generic_visit(node)
        self.enclosing.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.enclosing.append(node.name)
        self.generic_visit(node)
        self.enclosing.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _called_name(node.func)
        if name in _ACCESS_NAMES:
            self.found.add((self.enclosing[-1] if self.enclosing else "<module>", _ACCESS_NAMES[name]))
        self.generic_visit(node)


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        base = ast.unparse(func.value)
        return f"{base}.{func.attr}" if base in _MODULE_QUALIFIED else func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _observed_accesses() -> set[tuple[str, str, str]]:
    observed: set[tuple[str, str, str]] = set()
    for module in sorted(EVALUATOR_PACKAGE.glob("*.py")):
        visitor = _AccessVisitor()
        visitor.visit(ast.parse(module.read_text(encoding="utf-8")))
        observed.update((module.name, function, access) for function, access in visitor.found)
    return observed


def test_the_evaluator_owns_exactly_the_declared_filesystem_accesses() -> None:
    """Every read and write the evaluator executes is declared, and nothing else is."""

    declared = {(module, function, access) for module, function, access, _owner in OWNERSHIP}
    observed = _observed_accesses()

    assert observed - declared == set(), "undeclared evaluator filesystem access"
    assert declared - observed == set(), "declared evaluator filesystem access no longer exists"


def test_every_declared_access_has_exactly_one_owner() -> None:
    owners: dict[tuple[str, str, str], set[str]] = {}
    for module, function, access, owner in OWNERSHIP:
        owners.setdefault((module, function, access), set()).add(owner)
    assert all(len(value) == 1 for value in owners.values())
    assert {owner for _module, _function, _access, owner in OWNERSHIP} <= {
        CONFINED,
        GUARDED,
        DESCRIPTOR,
        CHILD,
        OWN_IMAGE,
    }


def test_no_evaluator_module_reads_or_writes_a_file_by_pathname() -> None:
    """The pathname-shaped accesses are exactly the two that provably cannot be redirected.

    ``Path.read_bytes`` survives in two places only: the child re-reading a module Python
    already imported (a regular file by construction, re-read guarded by the parent), and the
    sealed ``memfd`` this process created and sealed itself.  Everything else that reads or
    writes bytes does so through a descriptor whose every component was opened ``O_NOFOLLOW``.
    """

    pathname_accesses = {
        (module, function, access)
        for module, function, access in _observed_accesses()
        if access.startswith("Path.") or access == "open"
    }
    assert pathname_accesses == {
        ("production_child.py", "_production_files", "Path.read_bytes"),
        ("runtime.py", "_require_sealed_image", "Path.read_bytes"),
    }


def test_no_evaluator_module_creates_a_directory_by_pathname() -> None:
    """``mkdir(parents=True)`` accepts a symlinked intermediate component; ``os.mkdir`` with a
    ``dir_fd`` cannot.  Every directory the harness creates goes through the latter."""

    assert not {
        (module, function, access)
        for module, function, access in _observed_accesses()
        if access in {"Path.mkdir", "Path.makedirs", "os.makedirs"}
    }


@pytest.mark.parametrize("owner", [CONFINED, GUARDED, DESCRIPTOR, CHILD, OWN_IMAGE])
def test_the_ownership_document_explains_each_owner_class(owner: str) -> None:
    assert OWNERSHIP_DOC.is_file()
    assert owner in OWNERSHIP_DOC.read_text(encoding="utf-8")


def test_the_ownership_document_names_every_audited_module() -> None:
    text = OWNERSHIP_DOC.read_text(encoding="utf-8")
    modules = {module for module, _function, _access, _owner in OWNERSHIP}
    # These three execute no filesystem access of their own; the document says so explicitly.
    without_access = {"__init__.py", "models.py", "process.py", "write_guard.py"}
    assert modules == {path.name for path in EVALUATOR_PACKAGE.glob("*.py")} - without_access
    for module in sorted(modules):
        assert module in text
