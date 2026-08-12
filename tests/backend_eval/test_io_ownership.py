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
EVALUATOR_BOOTSTRAP = EVALUATOR_ROOT / "scripts" / "backend_eval_bootstrap.py"
OWNERSHIP_DOC = EVALUATOR_ROOT / "docs" / "backend-eval-io-ownership.md"


def _evaluator_sources() -> tuple[Path, ...]:
    """Every Python module executed by the evaluator, including its transport trust root."""

    return (*sorted(EVALUATOR_PACKAGE.glob("*.py")), EVALUATOR_BOOTSTRAP)

# --- what counts as a filesystem access ------------------------------------------------
#
# Descriptor primitives, every pathname-shaped ``pathlib`` and builtin access, and every
# production helper that reads a file the evaluation does not own.  A helper that only
# decodes or normalizes strings is not filesystem access and is deliberately absent.

_MODULE_QUALIFIED = (
    "os",
    "os.environ",
    "os.path",
    "psutil",
    "shutil",
    "subprocess",
    "_bootstrap_os",
    "provider",
)
_ACCESS_NAMES = {
    # --- namespace resolution and creation
    "os.open": "os.open",
    "os.fdopen": "os.fdopen",
    "os.mkdir": "os.mkdir",
    "os.makedirs": "os.makedirs",
    "os.scandir": "os.scandir",
    "os.listdir": "os.listdir",
    "os.walk": "os.walk",
    "open": "open",
    # --- namespace mutation
    "os.link": "os.link",
    "os.unlink": "os.unlink",
    "os.rename": "os.rename",
    "os.replace": "os.replace",
    "os.rmdir": "os.rmdir",
    "os.symlink": "os.symlink",
    "os.remove": "os.remove",
    # --- metadata and link inspection
    "os.stat": "os.stat",
    "os.lstat": "os.lstat",
    "os.fstat": "os.fstat",
    "os.readlink": "os.readlink",
    "os.access": "os.access",
    "os.chmod": "os.chmod",
    "os.fchmod": "os.fchmod",
    "os.path.realpath": "os.path.realpath",
    "os.path.islink": "os.path.islink",
    "os.path.isfile": "os.path.isfile",
    "os.path.isdir": "os.path.isdir",
    "os.path.exists": "os.path.exists",
    "os.path.getsize": "os.path.getsize",
    # --- descriptor byte movement and durability
    "os.read": "os.read",
    "os.write": "os.write",
    "os.pread": "os.pread",
    "os.pwrite": "os.pwrite",
    "os.lseek": "os.lseek",
    "os.truncate": "os.truncate",
    "os.ftruncate": "os.ftruncate",
    "os.fsync": "os.fsync",
    "os.fdatasync": "os.fdatasync",
    "read": "stream.read",
    "write": "stream.write",
    "flush": "stream.flush",
    # --- descriptor duplication and release
    "os.dup": "os.dup",
    "os.dup2": "os.dup2",
    "os.close": "os.close",
    "os.closerange": "os.closerange",
    # --- executable and path discovery
    "shutil.which": "shutil.which",
    "shutil.copy2": "shutil.copy2",
    "shutil.copytree": "shutil.copytree",
    "shutil.rmtree": "shutil.rmtree",
    "shutil.move": "shutil.move",
    # --- pathname-shaped pathlib access
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
    "lstat": "Path.lstat",
    "stat": "Path.stat",
    "is_file": "Path.is_file",
    "is_dir": "Path.is_dir",
    "is_symlink": "Path.is_symlink",
    "exists": "Path.exists",
    "resolve": "Path.resolve",
    "samefile": "Path.samefile",
    "symlink_to": "Path.symlink_to",
    "hardlink_to": "Path.hardlink_to",
    "unlink": "Path.unlink",
    "rename": "Path.rename",
    "replace": "Path.replace",
    "chmod": "Path.chmod",
    "readlink": "Path.readlink",
    # --- production helpers that read a file the evaluation does not own.  Every one of
    # these now appears only inside ``production_child.py``, which runs in its own
    # interpreter from a sealed image; a call to any of them anywhere else in the evaluator
    # fails the table, because an in-process call would compile production bytes the
    # evaluator identity has not yet named.
    "observe_file_digest": "production.observe_file_digest",
    "dependency_lock_digest": "production.dependency_lock_digest",
    "compute_build_identity": "production.compute_build_identity",
    "runtime_paths": "production.runtime_paths",
    "bounded_non_git_trust_inventory": "production.bounded_non_git_trust_inventory",
    "git_trust_inventory": "production.git_trust_inventory",
    "open_guarded_directory": "production.open_guarded_directory",
    "_inventory_from_candidates": "production._inventory_from_candidates",
    "_decode_git_path": "production._decode_git_path",
    # --- and the parent side of that boundary: every delegation to the bounded child.
    "run_production_helper": "delegated.production_child",
    # --- Phase 2's parent side of the candidate-process boundary.
    "provider.start": "delegated.candidate_process",
    # --- Phase 2 source and candidate lifecycle observation boundaries.
    "read_stable_source_text": "delegated.stable_source_read",
    "children": "psutil.Process.children",
    "create_time": "psutil.Process.create_time",
    "status": "psutil.Process.status",
    "psutil.process_iter": "psutil.process_iter",
    "os.getpgid": "os.getpgid",
    "os.killpg": "os.killpg",
    # --- The lifecycle poison proof's bounded, synchronously restored ambient mutation.
    "os.environ.get": "os.environ.get",
    "os.environ.update": "os.environ.update",
    "os.environ.pop": "os.environ.pop",
}

# --- the owner classes -----------------------------------------------------------------

CONFINED = "confined"
"""Reached component by component from an already-proven descriptor, ``O_NOFOLLOW`` throughout.

Every intermediate directory is opened from its parent's descriptor and every leaf name is
resolved relative to a descriptor, so no swapped ancestor can move the target.  The walk's
own starting point is a ``guarded`` root open, declared separately.
"""

GUARDED = "guarded"
"""One open of an absolute pathname with the strongest single-open guard, and no parent to walk from.

Two shapes, both honest about what they do *not* close.  A declared root directory is opened
``O_DIRECTORY``, which refuses a non-directory before any type-specific open handler runs but
proves nothing about the components above it -- it is where confinement starts, not something
confinement covers.  A regular file outside every owned root -- the CLI host interpreter, the
declared Git executable, the evaluator's own source closure -- is opened
``O_NOFOLLOW | O_NONBLOCK`` and proven regular by ``fstat`` on that same descriptor.
"""

DECLARED = "declared-path"
"""A pathname-shaped *observation* of a caller-declared path, used only to refuse.

Weaker than ``guarded`` and named separately for that reason: ``Path.is_file()``,
``Path.lstat()``, ``os.access``, and ``os.path.realpath`` resolve the name again, so a node
substituted after the observation is not caught by it.  Every row in this class only ever
*rejects* -- it never authorizes a byte to move, and the read or write that follows is owned
by a ``confined``, ``guarded``, or ``descriptor`` row that resolves nothing by name.
"""

DESCRIPTOR = "descriptor"
"""Operates on a descriptor this process already holds; there is no pathname to redirect."""

CHILD = "production-child"
"""Exact production semantics, executed in the bounded, killable, source-bound child."""

CANDIDATE_CHILD = "candidate-child"
"""A declared candidate process launched and reaped through production's process owner."""

SOURCE_READ = "source-read-delegation"
"""A source read delegated to the descriptor-confined stable-source reader."""

PROCESS_OBSERVATION = "owned-process-observation"
"""An exact PID/create-time/process-group observation of an evaluator-owned child."""

PROCESS_SIGNAL = "owned-process-group-signal"
"""A process-group signal issued only by the owner of that bounded child lifecycle."""

TEMP_ENVIRONMENT = "temporary-process-environment"
"""A bounded, restored process-environment mutation used only for the poison proof."""

OWN_IMAGE = "own-image"
"""Reads or writes this process's own sealed ``memfd``; no attacker surface."""

# Descriptor primitives take a descriptor, never a name.  They are enumerated as rows like
# everything else, *and* proven mechanically below to receive a descriptor-shaped argument, so
# "it operates on a descriptor" is a checked claim rather than an assertion in a docstring.
_DESCRIPTOR_PRIMITIVES = {
    "os.close",
    "os.closerange",
    "os.dup",
    "os.fchmod",
    "os.fdatasync",
    "os.fdopen",
    "os.fstat",
    "os.fsync",
    "os.ftruncate",
    "os.lseek",
    "os.pread",
    "os.pwrite",
    "os.read",
    "os.scandir",
    "os.write",
}

# The closed set of expressions that may supply one.  ``os.open`` and ``os.dup`` are the
# kernel's own; ``fileno`` is the descriptor a stream already wraps; the two evaluator
# openers are confined walks declared in the table below and typed ``-> int``.
_DESCRIPTOR_SOURCES = {
    "os.open",
    "os.dup",
    "fileno",
    "_open_owned_directory",
    "_open_confined_child",
}

# --- the table -------------------------------------------------------------------------
#
# (module, enclosing function, access, owner).  Line numbers are deliberately absent: they
# churn, and the audit question is who owns the access, not where it sits today.

OWNERSHIP: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        # --- __init__.py
        # --- admission.py
        ("admission.py", "__post_init__", "Path.is_dir", DECLARED),
        ("admission.py", "<module>", "stream.write", DESCRIPTOR),
        ("admission.py", "_collect_artifact_entries", "os.close", DESCRIPTOR),
        ("admission.py", "_collect_artifact_entries", "os.lstat", CONFINED),
        ("admission.py", "_collect_artifact_entries", "os.open", CONFINED),
        ("admission.py", "_collect_artifact_entries", "os.scandir", DESCRIPTOR),
        ("admission.py", "_open_declared_root", "os.open", GUARDED),
        ("admission.py", "_open_owned_walk", "os.close", DESCRIPTOR),
        ("admission.py", "_open_owned_walk", "os.open", CONFINED),
        ("admission.py", "_read_artifact_bytes", "os.close", DESCRIPTOR),
        ("admission.py", "_read_artifact_bytes", "os.fdopen", DESCRIPTOR),
        ("admission.py", "_read_artifact_bytes", "os.fstat", DESCRIPTOR),
        ("admission.py", "_read_artifact_bytes", "os.open", CONFINED),
        ("admission.py", "_read_artifact_bytes", "stream.read", DESCRIPTOR),
        ("admission.py", "artifact_tree_digest", "os.close", DESCRIPTOR),
        ("admission.py", "cleanup", "os.close", DESCRIPTOR),
        ("admission.py", "cleanup", "os.fsync", DESCRIPTOR),
        ("admission.py", "cleanup", "os.unlink", CONFINED),
        # --- backend_eval_bootstrap.py
        ("backend_eval_bootstrap.py", "_bootstrap_command", "stream.write", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_bound_psutil_sources", "Path.is_file", DECLARED),
        ("backend_eval_bootstrap.py", "_bound_psutil_sources", "os.close", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_bound_psutil_sources", "os.scandir", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_build_evaluator_source_image", "os.close", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_build_evaluator_source_image", "os.scandir", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_closed_startup", "os.path.realpath", DECLARED),
        ("backend_eval_bootstrap.py", "_kill_evaluator_group", "os.getpgid", PROCESS_OBSERVATION),
        ("backend_eval_bootstrap.py", "_kill_evaluator_group", "os.killpg", PROCESS_SIGNAL),
        ("backend_eval_bootstrap.py", "_module_file_location", "Path.is_file", DECLARED),
        ("backend_eval_bootstrap.py", "_open_absolute_directory", "os.close", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_open_absolute_directory", "os.open", CONFINED),
        ("backend_eval_bootstrap.py", "_open_filesystem_root", "os.open", GUARDED),
        ("backend_eval_bootstrap.py", "_open_relative_directory", "os.close", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_open_relative_directory", "os.dup", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_open_relative_directory", "os.open", CONFINED),
        ("backend_eval_bootstrap.py", "_read_owned_source", "os.close", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_read_relative_file", "os.close", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_read_relative_file", "os.fstat", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_read_relative_file", "os.open", CONFINED),
        ("backend_eval_bootstrap.py", "_read_relative_file", "os.read", DESCRIPTOR),
        ("backend_eval_bootstrap.py", "_run_sealed_evaluator", "os.close", OWN_IMAGE),
        ("backend_eval_bootstrap.py", "_run_sealed_protocol", "os.close", OWN_IMAGE),
        ("backend_eval_bootstrap.py", "_sealed_evaluator_image", "os.close", OWN_IMAGE),
        ("backend_eval_bootstrap.py", "_sealed_evaluator_image", "os.pread", OWN_IMAGE),
        ("backend_eval_bootstrap.py", "_sealed_evaluator_image", "os.write", OWN_IMAGE),
        ("backend_eval_bootstrap.py", "_source_imports", "Path.walk", DECLARED),
        ("backend_eval_bootstrap.py", "main", "stream.write", DESCRIPTOR),
        # --- candidate_lock.py
        ("candidate_lock.py", "_artifact_directory", "os.close", DESCRIPTOR),
        ("candidate_lock.py", "_artifact_directory", "os.open", GUARDED),
        ("candidate_lock.py", "_ensure_cache_directory", "os.close", DESCRIPTOR),
        ("candidate_lock.py", "_fsync_directory", "os.fsync", DESCRIPTOR),
        ("candidate_lock.py", "_fsync_file", "os.fsync", DESCRIPTOR),
        ("candidate_lock.py", "_node_exists", "os.lstat", CONFINED),
        ("candidate_lock.py", "_open_owned_directory", "os.close", DESCRIPTOR),
        ("candidate_lock.py", "_open_owned_directory", "os.fchmod", DESCRIPTOR),
        ("candidate_lock.py", "_open_owned_directory", "os.mkdir", CONFINED),
        ("candidate_lock.py", "_open_owned_directory", "os.open", CONFINED),
        ("candidate_lock.py", "_open_regular_artifact", "os.close", DESCRIPTOR),
        ("candidate_lock.py", "_open_regular_artifact", "os.fstat", DESCRIPTOR),
        ("candidate_lock.py", "_open_regular_artifact", "os.open", CONFINED),
        ("candidate_lock.py", "_purge_directory", "Path.is_dir", DECLARED),
        ("candidate_lock.py", "_purge_directory", "os.close", DESCRIPTOR),
        ("candidate_lock.py", "_purge_directory", "os.open", CONFINED),
        ("candidate_lock.py", "_purge_directory", "os.rmdir", CONFINED),
        ("candidate_lock.py", "_purge_directory", "os.scandir", DESCRIPTOR),
        ("candidate_lock.py", "_purge_node", "os.lstat", CONFINED),
        ("candidate_lock.py", "_purge_quarantined_nodes", "os.scandir", DESCRIPTOR),
        ("candidate_lock.py", "_read_descriptor", "os.fdopen", DESCRIPTOR),
        ("candidate_lock.py", "_read_descriptor", "stream.read", DESCRIPTOR),
        ("candidate_lock.py", "_reject_non_regular", "os.lstat", CONFINED),
        ("candidate_lock.py", "_remove_artifact", "os.unlink", CONFINED),
        ("candidate_lock.py", "_rename_artifact", "os.rename", CONFINED),
        ("candidate_lock.py", "_resolution_lock", "os.close", DESCRIPTOR),
        ("candidate_lock.py", "_resolution_lock", "os.fchmod", DESCRIPTOR),
        ("candidate_lock.py", "_resolution_lock", "os.open", CONFINED),
        ("candidate_lock.py", "_validate_directory", "Path.is_dir", DECLARED),
        ("candidate_lock.py", "_validate_executable", "Path.is_file", DECLARED),
        ("candidate_lock.py", "_validate_executable", "os.access", DECLARED),
        ("candidate_lock.py", "_write_artifact", "os.fchmod", DESCRIPTOR),
        ("candidate_lock.py", "_write_artifact", "os.fdopen", DESCRIPTOR),
        ("candidate_lock.py", "_write_artifact", "os.open", CONFINED),
        ("candidate_lock.py", "_write_artifact", "os.replace", CONFINED),
        ("candidate_lock.py", "_write_artifact", "stream.flush", DESCRIPTOR),
        ("candidate_lock.py", "_write_artifact", "stream.write", DESCRIPTOR),
        # --- identity.py
        ("identity.py", "_read_regular_file", "os.close", DESCRIPTOR),
        ("identity.py", "_read_regular_file", "os.fdopen", DESCRIPTOR),
        ("identity.py", "_read_regular_file", "os.fstat", DESCRIPTOR),
        ("identity.py", "_read_regular_file", "os.open", GUARDED),
        ("identity.py", "_read_regular_file", "stream.read", DESCRIPTOR),
        ("identity.py", "_image_matches_current_checkout", "os.scandir", DECLARED),
        ("identity.py", "_require_no_shadowed_module", "Path.resolve", DECLARED),
        ("identity.py", "_source_closure", "os.scandir", DESCRIPTOR),
        ("identity.py", "capture_evaluator_identity", "os.path.realpath", DECLARED),
        # --- manifests.py
        ("manifests.py", "_child_inventory", "delegated.production_child", CHILD),
        ("manifests.py", "_digest_chunk", "delegated.production_child", CHILD),
        ("manifests.py", "_open_declared_corpus_root", "os.open", GUARDED),
        ("manifests.py", "_open_metadata_directory", "os.close", DESCRIPTOR),
        ("manifests.py", "_open_metadata_directory", "os.open", CONFINED),
        ("manifests.py", "_open_source_filesystem_root", "os.open", GUARDED),
        ("manifests.py", "_open_stable_source_root", "os.close", DESCRIPTOR),
        ("manifests.py", "_open_stable_source_root", "os.open", CONFINED),
        ("manifests.py", "_resolved", "Path.resolve", DECLARED),
        ("manifests.py", "_require_stable_source_root", "os.fstat", DESCRIPTOR),
        ("manifests.py", "_require_stable_source_root", "os.readlink", DESCRIPTOR),
        ("manifests.py", "_require_stable_source_root", "os.stat", CONFINED),
        ("manifests.py", "_require_stable_source_directories", "os.fstat", DESCRIPTOR),
        ("manifests.py", "_require_stable_source_directories", "os.stat", CONFINED),
        ("manifests.py", "_lstat", "Path.lstat", DECLARED),
        ("manifests.py", "_require_directory", "Path.lstat", DECLARED),
        ("manifests.py", "_scan_remainder", "os.close", DESCRIPTOR),
        ("manifests.py", "_walk_metadata_root", "os.close", DESCRIPTOR),
        ("manifests.py", "_walk_metadata_root", "os.lstat", CONFINED),
        ("manifests.py", "_walk_metadata_root", "os.readlink", CONFINED),
        ("manifests.py", "_walk_metadata_root", "os.scandir", DESCRIPTOR),
        ("manifests.py", "_walk_remainder", "os.close", DESCRIPTOR),
        ("manifests.py", "_walk_remainder", "os.lstat", CONFINED),
        ("manifests.py", "_walk_remainder", "os.open", CONFINED),
        ("manifests.py", "_walk_remainder", "os.readlink", CONFINED),
        ("manifests.py", "_walk_remainder", "os.scandir", DESCRIPTOR),
        ("manifests.py", "read_stable_source_text", "os.close", DESCRIPTOR),
        ("manifests.py", "read_stable_source_text", "os.fstat", DESCRIPTOR),
        ("manifests.py", "read_stable_source_text", "os.open", CONFINED),
        ("manifests.py", "read_stable_source_text", "os.read", DESCRIPTOR),
        ("manifests.py", "read_stable_source_text", "os.stat", CONFINED),
        # --- process.py
        ("process.py", "bound_executable", "os.access", DECLARED),
        ("process.py", "bound_executable", "os.close", DESCRIPTOR),
        ("process.py", "bound_executable", "os.fstat", DESCRIPTOR),
        ("process.py", "bound_executable", "os.open", GUARDED),
        ("process.py", "_kill_process_group", "os.getpgid", PROCESS_OBSERVATION),
        ("process.py", "_kill_process_group", "os.killpg", PROCESS_SIGNAL),
        ("process.py", "sealed_image", "os.close", DESCRIPTOR),
        ("process.py", "sealed_image", "os.pread", OWN_IMAGE),
        ("process.py", "sealed_image", "os.write", OWN_IMAGE),
        # --- publish.py
        # Every close in this module goes through ``_close_payload`` (pre-link, actionable)
        # or ``_release_descriptor`` (post-durability, deliberately not propagated).
        ("publish.py", "_close_payload", "os.close", DESCRIPTOR),
        ("publish.py", "_open_owned_child", "os.fchmod", DESCRIPTOR),
        ("publish.py", "_open_owned_child", "os.mkdir", CONFINED),
        ("publish.py", "_open_owned_child", "os.open", CONFINED),
        ("publish.py", "_open_target_directory", "os.open", GUARDED),
        ("publish.py", "_publication_lock", "os.fchmod", DESCRIPTOR),
        ("publish.py", "_publication_lock", "os.open", CONFINED),
        ("publish.py", "_release_descriptor", "os.close", DESCRIPTOR),
        ("publish.py", "_remove_owned_names", "os.unlink", CONFINED),
        ("publish.py", "_replace_temporary", "os.unlink", CONFINED),
        ("publish.py", "_stage_payload", "os.fchmod", DESCRIPTOR),
        ("publish.py", "_stage_payload", "os.fsync", DESCRIPTOR),
        ("publish.py", "_stage_payload", "os.open", CONFINED),
        ("publish.py", "_sync_directory", "os.fsync", DESCRIPTOR),
        ("publish.py", "_write_all", "os.write", DESCRIPTOR),
        ("publish.py", "publish_immutable_record", "os.link", CONFINED),
        # --- pyrefly_probe.py
        ("pyrefly_probe.py", "_run_capability_probe", "delegated.stable_source_read", SOURCE_READ),
        # --- pyright_probe.py
        ("pyright_probe.py", "run_pyright_capability_probe", "delegated.stable_source_read", SOURCE_READ),
        # --- protocol.py
        ("protocol.py", "_cleanup_partial_launch", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol.py", "_cleanup_partial_launch", "os.killpg", PROCESS_SIGNAL),
        ("protocol.py", "run_protocol_probe", "delegated.candidate_process", CANDIDATE_CHILD),
        # --- protocol_lifecycle.py
        ("protocol_lifecycle.py", "_cold_diagnostics", "delegated.stable_source_read", SOURCE_READ),
        ("protocol_lifecycle.py", "_direct_children", "psutil.Process.children", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_direct_children", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_discover_new_owned_child", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_discover_new_owned_child", "psutil.Process.children", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_discover_new_owned_child", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_live_process_group_members", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_live_process_group_members", "psutil.process_iter", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_same_process_alive", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_same_process_alive", "psutil.Process.status", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_signal_captured_process", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_signal_captured_process", "os.killpg", PROCESS_SIGNAL),
        ("protocol_lifecycle.py", "_signal_captured_process", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_temporary_environment", "os.environ.get", TEMP_ENVIRONMENT),
        ("protocol_lifecycle.py", "_temporary_environment", "os.environ.pop", TEMP_ENVIRONMENT),
        ("protocol_lifecycle.py", "_temporary_environment", "os.environ.update", TEMP_ENVIRONMENT),
        # --- protocol_parent.py
        ("protocol_parent.py", "_close_descriptor", "os.close", DESCRIPTOR),
        ("protocol_parent.py", "_open_filesystem_root", "os.close", DESCRIPTOR),
        ("protocol_parent.py", "_open_filesystem_root", "os.open", GUARDED),
        ("protocol_parent.py", "_read_exact_regular_file", "os.close", DESCRIPTOR),
        ("protocol_parent.py", "_read_exact_regular_file", "os.fdopen", DESCRIPTOR),
        ("protocol_parent.py", "_read_exact_regular_file", "os.fstat", DESCRIPTOR),
        ("protocol_parent.py", "_read_exact_regular_file", "os.open", CONFINED),
        ("protocol_parent.py", "_read_exact_regular_file", "stream.read", DESCRIPTOR),
        # --- protocol_witness.py
        ("protocol_witness.py", "_checked_open", "os.close", DESCRIPTOR),
        ("protocol_witness.py", "_external_relative_path", "Path.resolve", DECLARED),
        ("protocol_witness.py", "_external_transformers_location", "Path.resolve", DECLARED),
        ("protocol_witness.py", "_open_owned_run_root", "os.close", DESCRIPTOR),
        ("protocol_witness.py", "_open_owned_run_root", "os.fstat", DESCRIPTOR),
        ("protocol_witness.py", "_open_owned_run_root", "os.open", GUARDED),
        ("protocol_witness.py", "cleanup", "os.close", DESCRIPTOR),
        ("protocol_witness.py", "cleanup", "os.fsync", DESCRIPTOR),
        ("protocol_witness.py", "cleanup", "os.rmdir", CONFINED),
        ("protocol_witness.py", "cleanup", "os.stat", CONFINED),
        ("protocol_witness.py", "cleanup", "os.unlink", CONFINED),
        ("protocol_witness.py", "create", "os.chmod", CONFINED),
        ("protocol_witness.py", "create", "os.close", DESCRIPTOR),
        ("protocol_witness.py", "create", "os.fchmod", DESCRIPTOR),
        ("protocol_witness.py", "create", "os.fstat", DESCRIPTOR),
        ("protocol_witness.py", "create", "os.fsync", DESCRIPTOR),
        ("protocol_witness.py", "create", "os.mkdir", CONFINED),
        ("protocol_witness.py", "create", "os.open", CONFINED),
        ("protocol_witness.py", "create", "os.rmdir", CONFINED),
        ("protocol_witness.py", "create", "os.unlink", CONFINED),
        ("protocol_witness.py", "create", "os.write", DESCRIPTOR),
        ("protocol_witness.py", "verify", "os.close", DESCRIPTOR),
        ("protocol_witness.py", "verify", "os.fstat", DESCRIPTOR),
        ("protocol_witness.py", "verify", "os.open", CONFINED),
        ("protocol_witness.py", "verify", "os.read", DESCRIPTOR),
        ("protocol_witness.py", "verify", "os.stat", CONFINED),
        # --- production_child.py
        ("production_child.py", "_bounded_non_git_inventory", "production.bounded_non_git_trust_inventory", CHILD),
        ("production_child.py", "_git_inventory_from_bytes", "Path.resolve", CHILD),
        ("production_child.py", "_git_inventory_from_bytes", "production._decode_git_path", CHILD),
        ("production_child.py", "_git_inventory_from_bytes", "production._inventory_from_candidates", CHILD),
        ("production_child.py", "_load_image", "os.fstat", OWN_IMAGE),
        ("production_child.py", "_load_image", "os.pread", OWN_IMAGE),
        ("production_child.py", "_observe_file_digests", "production.observe_file_digest", CHILD),
        ("production_child.py", "_production_identity", "production.compute_build_identity", CHILD),
        ("production_child.py", "_production_identity", "production.dependency_lock_digest", CHILD),
        ("production_child.py", "_production_identity", "production.runtime_paths", CHILD),
        ("production_child.py", "main", "os.path.realpath", DECLARED),
        ("production_child.py", "main", "stream.flush", DESCRIPTOR),
        ("production_child.py", "main", "stream.read", DESCRIPTOR),
        ("production_child.py", "main", "stream.write", DESCRIPTOR),
        # --- production_helper.py
        ("production_helper.py", "_open_owner_root", "os.open", GUARDED),
        ("production_helper.py", "_read_owned_file", "os.close", DESCRIPTOR),
        ("production_helper.py", "_read_owned_file", "os.dup", DESCRIPTOR),
        ("production_helper.py", "_read_owned_file", "os.fdopen", DESCRIPTOR),
        ("production_helper.py", "_read_owned_file", "os.fstat", DESCRIPTOR),
        ("production_helper.py", "_read_owned_file", "os.open", CONFINED),
        ("production_helper.py", "_read_owned_file", "stream.read", DESCRIPTOR),
        ("production_helper.py", "run_production_helper", "os.close", DESCRIPTOR),
        # --- production_identity.py
        ("production_identity.py", "_run_production_helpers", "delegated.production_child", CHILD),
        ("production_identity.py", "_read_guarded", "os.close", DESCRIPTOR),
        ("production_identity.py", "_read_guarded", "os.fdopen", DESCRIPTOR),
        ("production_identity.py", "_read_guarded", "os.fstat", DESCRIPTOR),
        ("production_identity.py", "_read_guarded", "os.open", CONFINED),
        ("production_identity.py", "_read_guarded", "stream.read", DESCRIPTOR),
        ("production_identity.py", "_read_identity_inputs", "os.close", DESCRIPTOR),
        ("production_identity.py", "_read_identity_inputs", "os.open", GUARDED),
        ("production_identity.py", "capture_production_identity", "Path.resolve", DECLARED),
        # --- runtime.py
        ("runtime.py", "_capture_executable_identity", "os.path.realpath", DECLARED),
        ("runtime.py", "_create_runtime_directories", "os.close", DESCRIPTOR),
        ("runtime.py", "_fsync", "os.fsync", DESCRIPTOR),
        ("runtime.py", "_normalize_owned_modes", "os.close", DESCRIPTOR),
        ("runtime.py", "_normalize_owned_modes", "os.fchmod", DESCRIPTOR),
        ("runtime.py", "_normalize_owned_modes", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_observe_external_executable_resolution", "os.close", DESCRIPTOR),
        ("runtime.py", "_observe_external_executable_resolution", "os.dup", DESCRIPTOR),
        ("runtime.py", "_observe_external_executable_resolution", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_observe_external_executable_resolution", "os.open", CONFINED),
        ("runtime.py", "_observe_external_executable_resolution", "os.readlink", CONFINED),
        ("runtime.py", "_observe_external_executable_resolution", "os.stat", CONFINED),
        ("runtime.py", "_open_external_resolution_root", "os.open", GUARDED),
        ("runtime.py", "_observe_regular_from_parent", "os.close", DESCRIPTOR),
        ("runtime.py", "_observe_regular_from_parent", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_observe_regular_from_parent", "os.open", CONFINED),
        ("runtime.py", "_observe_regular_from_parent", "os.stat", CONFINED),
        ("runtime.py", "_observe_runtime_directory", "os.close", DESCRIPTOR),
        ("runtime.py", "_observe_runtime_directory", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_observe_runtime_regular_file", "os.close", DESCRIPTOR),
        ("runtime.py", "_observe_runtime_symlink", "os.close", DESCRIPTOR),
        ("runtime.py", "_observe_runtime_symlink", "os.readlink", CONFINED),
        ("runtime.py", "_observe_runtime_symlink", "os.stat", CONFINED),
        ("runtime.py", "_open_confined_child", "os.close", DESCRIPTOR),
        ("runtime.py", "_open_confined_child", "os.fchmod", DESCRIPTOR),
        ("runtime.py", "_open_confined_child", "os.mkdir", CONFINED),
        ("runtime.py", "_open_confined_child", "os.open", CONFINED),
        ("runtime.py", "_open_confined_directory", "os.close", DESCRIPTOR),
        ("runtime.py", "_open_confined_directory", "os.open", CONFINED),
        ("runtime.py", "_open_confined_directory_chain", "os.close", DESCRIPTOR),
        ("runtime.py", "_open_confined_directory_chain", "os.dup", DESCRIPTOR),
        ("runtime.py", "_open_confined_existing_child", "os.open", CONFINED),
        ("runtime.py", "_open_confined_relpath", "os.close", DESCRIPTOR),
        ("runtime.py", "_open_confined_relpath", "os.dup", DESCRIPTOR),
        ("runtime.py", "_open_existing_confined_directory", "os.close", DESCRIPTOR),
        ("runtime.py", "_open_existing_confined_directory", "os.open", CONFINED),
        ("runtime.py", "_open_owned_write_leaf", "os.open", CONFINED),
        ("runtime.py", "_physical_path", "os.readlink", DESCRIPTOR),
        ("runtime.py", "_physical_prefix", "os.path.realpath", DECLARED),
        ("runtime.py", "_prepare", "os.close", DESCRIPTOR),
        ("runtime.py", "_publish_manifest", "os.replace", CONFINED),
        ("runtime.py", "_purge_directory_contents", "Path.is_dir", DECLARED),
        ("runtime.py", "_purge_directory_contents", "os.close", DESCRIPTOR),
        ("runtime.py", "_purge_directory_contents", "os.open", CONFINED),
        ("runtime.py", "_purge_directory_contents", "os.rmdir", CONFINED),
        ("runtime.py", "_purge_directory_contents", "os.scandir", DESCRIPTOR),
        ("runtime.py", "_purge_directory_contents", "os.unlink", CONFINED),
        ("runtime.py", "_purge_runtime_root", "os.rmdir", CONFINED),
        ("runtime.py", "_read_confined_file", "os.close", DESCRIPTOR),
        ("runtime.py", "_read_owned_descriptor", "os.fdopen", DESCRIPTOR),
        ("runtime.py", "_read_owned_descriptor", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_read_owned_descriptor", "stream.read", DESCRIPTOR),
        ("runtime.py", "_read_regular_file", "os.close", DESCRIPTOR),
        ("runtime.py", "_read_regular_file", "os.fdopen", DESCRIPTOR),
        ("runtime.py", "_read_regular_file", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_read_regular_file", "os.open", GUARDED),
        ("runtime.py", "_read_regular_file", "stream.read", DESCRIPTOR),
        ("runtime.py", "_reprove_external_resolution_chain", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_reprove_external_resolution_chain", "os.readlink", CONFINED),
        ("runtime.py", "_reprove_external_resolution_chain", "os.stat", CONFINED),
        ("runtime.py", "_require_directory", "Path.is_dir", DECLARED),
        ("runtime.py", "_require_executable", "Path.is_file", DECLARED),
        ("runtime.py", "_require_executable", "os.access", DECLARED),
        ("runtime.py", "_require_existing_regular_file", "Path.is_file", DECLARED),
        ("runtime.py", "_require_existing_regular_file", "Path.is_symlink", DECLARED),
        ("runtime.py", "_require_open_root", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_require_open_root", "os.lstat", CONFINED),
        ("runtime.py", "_require_owned_directory", "Path.lstat", DECLARED),
        ("runtime.py", "_require_owned_modes", "os.close", DESCRIPTOR),
        ("runtime.py", "_require_owned_modes", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_require_regular_executable_inside", "Path.lstat", DECLARED),
        ("runtime.py", "_require_regular_executable_inside", "os.access", DECLARED),
        ("runtime.py", "_require_regular_executable_inside", "os.path.realpath", DECLARED),
        ("runtime.py", "_require_regular_file", "Path.is_file", DECLARED),
        ("runtime.py", "_require_regular_file", "Path.is_symlink", DECLARED),
        ("runtime.py", "_require_sealed_image", "Path.read_bytes", OWN_IMAGE),
        ("runtime.py", "_require_venv_interpreter", "Path.is_file", DECLARED),
        ("runtime.py", "_require_venv_interpreter", "os.access", DECLARED),
        ("runtime.py", "_require_venv_interpreter", "os.path.realpath", DECLARED),
        ("runtime.py", "_verify_manifest_candidate_executables", "os.close", DESCRIPTOR),
        ("runtime.py", "_verify_manifest_candidate_executables", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_verify_manifest_external_identity", "os.access", DECLARED),
        ("runtime.py", "_verify_manifest_external_identity", "os.path.realpath", DECLARED),
        ("runtime.py", "_verify_selected_python_link", "os.close", DESCRIPTOR),
        ("runtime.py", "_verify_selected_python_link", "os.readlink", CONFINED),
        ("runtime.py", "_runtime_lock", "os.close", DESCRIPTOR),
        ("runtime.py", "_runtime_lock", "os.open", CONFINED),
        ("runtime.py", "_scandir_names", "os.scandir", DESCRIPTOR),
        ("runtime.py", "_write_confined_file", "os.close", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "os.fchmod", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "os.fdopen", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "os.fstat", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "os.fsync", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "os.ftruncate", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "stream.flush", DESCRIPTOR),
        ("runtime.py", "_write_owned_descriptor", "stream.write", DESCRIPTOR),
        ("runtime.py", "runtime_manifest_digest", "os.close", DESCRIPTOR),
        ("runtime.py", "load_prepared_candidate_runtime", "os.close", DESCRIPTOR),
        ("runtime.py", "load_prepared_candidate_runtime", "os.open", CONFINED),
        # --- source_binding.py
        ("source_binding.py", "_module_paths", "os.path.realpath", DECLARED),
        ("source_binding.py", "_read_regular_file", "os.close", DESCRIPTOR),
        ("source_binding.py", "_read_regular_file", "os.fdopen", DESCRIPTOR),
        ("source_binding.py", "_read_regular_file", "os.fstat", DESCRIPTOR),
        ("source_binding.py", "_read_regular_file", "os.open", GUARDED),
        ("source_binding.py", "_read_regular_file", "stream.read", DESCRIPTOR),
        ("source_binding.py", "bind_production_source", "Path.resolve", DECLARED),
        ("source_binding.py", "bind_production_source", "os.path.realpath", DECLARED),
        # --- source_image.py
        ("source_image.py", "_sealed_descriptor_bytes", "os.fstat", OWN_IMAGE),
        ("source_image.py", "_sealed_descriptor_bytes", "os.pread", OWN_IMAGE),
        ("source_image.py", "_source_image_bytes", "os.fstat", OWN_IMAGE),
        ("source_image.py", "_source_image_bytes", "os.pread", OWN_IMAGE),
        ("source_image.py", "_verified_context", "os.fstat", OWN_IMAGE),
        ("source_image.py", "dependency_source_files", "stream.read", OWN_IMAGE),
        ("source_image.py", "evaluation_owner_root", "Path.resolve", DECLARED),
        ("source_image.py", "evaluator_source_files", "stream.read", OWN_IMAGE),
        ("source_image.py", "production_source_files", "stream.read", OWN_IMAGE),
        # --- ty_probe.py
        ("ty_probe.py", "run_ty_capability_probe", "delegated.stable_source_read", SOURCE_READ),
        ("ty_probe.py", "initialize_params", "Path.is_dir", DECLARED),
        ("ty_probe.py", "initialize_params", "Path.resolve", DECLARED),
        # --- write_guard.py
        ("write_guard.py", "_hashed_remainder_record", "Path.lstat", DECLARED),
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
        if base == "_bootstrap_os":
            base = "os"
        return f"{base}.{func.attr}" if base in _MODULE_QUALIFIED else func.attr
    if isinstance(func, ast.Name):
        if _ACCESS_NAMES.get(func.id, "").startswith("Path."):
            return None
        return func.id
    return None


def _observed_accesses() -> set[tuple[str, str, str]]:
    observed: set[tuple[str, str, str]] = set()
    for module in _evaluator_sources():
        visitor = _AccessVisitor()
        visitor.visit(ast.parse(module.read_text(encoding="utf-8")))
        observed.update((module.name, function, access) for function, access in visitor.found)
    return observed


def test_the_collector_does_not_treat_dataclass_replace_as_a_path_replace() -> None:
    """Only a receiver-qualified ``.replace()`` can be a pathlib namespace mutation."""

    visitor = _AccessVisitor()
    visitor.visit(
        ast.parse(
            "from dataclasses import replace\n"
            "def updated(value):\n"
            "    return replace(value, field=1)\n"
        )
    )

    assert visitor.found == set()


def test_the_evaluator_owns_exactly_the_declared_filesystem_accesses() -> None:
    """Every read and write the evaluator executes is declared, and nothing else is."""

    declared = {(module, function, access) for module, function, access, _owner in OWNERSHIP}
    observed = _observed_accesses()

    assert observed - declared == set(), "undeclared evaluator filesystem access"
    assert declared - observed == set(), "declared evaluator filesystem access no longer exists"


def test_candidate_process_delegation_is_structurally_collected_and_owned() -> None:
    candidate_process = ("protocol.py", "run_protocol_probe", "delegated.candidate_process")

    assert candidate_process in _observed_accesses()
    assert (*candidate_process, "candidate-child") in OWNERSHIP


def test_lifecycle_process_and_environment_surfaces_have_exact_owners() -> None:
    lifecycle = {
        row for row in OWNERSHIP if row[0] == "protocol_lifecycle.py"
    }
    assert lifecycle == {
        ("protocol_lifecycle.py", "_cold_diagnostics", "delegated.stable_source_read", SOURCE_READ),
        ("protocol_lifecycle.py", "_direct_children", "psutil.Process.children", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_direct_children", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_discover_new_owned_child", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_discover_new_owned_child", "psutil.Process.children", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_discover_new_owned_child", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_live_process_group_members", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_live_process_group_members", "psutil.process_iter", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_same_process_alive", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_same_process_alive", "psutil.Process.status", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_signal_captured_process", "os.getpgid", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_signal_captured_process", "os.killpg", PROCESS_SIGNAL),
        ("protocol_lifecycle.py", "_signal_captured_process", "psutil.Process.create_time", PROCESS_OBSERVATION),
        ("protocol_lifecycle.py", "_temporary_environment", "os.environ.get", TEMP_ENVIRONMENT),
        ("protocol_lifecycle.py", "_temporary_environment", "os.environ.pop", TEMP_ENVIRONMENT),
        ("protocol_lifecycle.py", "_temporary_environment", "os.environ.update", TEMP_ENVIRONMENT),
    }


def test_every_declared_access_has_exactly_one_owner() -> None:
    owners: dict[tuple[str, str, str], set[str]] = {}
    for module, function, access, owner in OWNERSHIP:
        owners.setdefault((module, function, access), set()).add(owner)
    assert all(len(value) == 1 for value in owners.values())
    assert {owner for _module, _function, _access, owner in OWNERSHIP} <= {
        CONFINED,
        GUARDED,
        DECLARED,
        DESCRIPTOR,
        CHILD,
        CANDIDATE_CHILD,
        SOURCE_READ,
        PROCESS_OBSERVATION,
        PROCESS_SIGNAL,
        TEMP_ENVIRONMENT,
        OWN_IMAGE,
    }


def test_no_evaluator_module_reads_or_writes_a_file_by_pathname() -> None:
    """The one pathname-shaped byte access left is the one that cannot be redirected.

    ``Path.read_bytes`` survives in exactly one place: the sealed ``memfd`` this process
    created and sealed itself.  Everything else that reads or writes bytes does so through a
    descriptor whose every component was opened ``O_NOFOLLOW``, including the production
    helpers, which now import from a sealed source image instead of re-reading their own
    files from disk.
    """

    pathname_accesses = {
        (module, function, access)
        for module, function, access in _observed_accesses()
        if access in {"Path.read_bytes", "Path.read_text", "Path.write_bytes", "Path.write_text", "open"}
    }
    assert pathname_accesses == {("runtime.py", "_require_sealed_image", "Path.read_bytes")}


def test_no_evaluator_module_creates_a_directory_by_pathname() -> None:
    """``mkdir(parents=True)`` accepts a symlinked intermediate component; ``os.mkdir`` with a
    ``dir_fd`` cannot.  Every directory the harness creates goes through the latter."""

    assert not {
        (module, function, access)
        for module, function, access in _observed_accesses()
        if access in {"Path.mkdir", "Path.makedirs", "os.makedirs"}
    }


def test_no_evaluator_module_discovers_an_executable_from_the_ambient_path() -> None:
    """``shutil.which`` answers from whatever ``PATH`` the ambient process carries.

    Every other input of this evaluation refuses ambient control, so the executables it runs
    are declared absolute pathnames, proven regular through one descriptor before the child
    starts.  A reintroduced ``which`` fails here.
    """

    assert not {
        (module, function, access)
        for module, function, access in _observed_accesses()
        if access == "shutil.which"
    }


def test_every_descriptor_primitive_receives_a_descriptor_and_never_a_pathname() -> None:
    """The descriptor class is proven mechanically, not asserted in a docstring.

    Every call to a descriptor primitive is checked structurally: its descriptor argument is a
    plain name, attribute, subscript, or the result of a call that returns a descriptor --
    never a string literal, an f-string, a ``Path(...)``, a ``str(...)``, or a ``/`` join.  A
    row that claimed ``descriptor`` while handing the call a constructed pathname fails here,
    so the class cannot be used to hide a pathname-shaped access.
    """

    offenders: list[str] = []
    for module in _evaluator_sources():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node.func)
            if name not in _DESCRIPTOR_PRIMITIVES or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name | ast.Attribute | ast.Subscript):
                continue
            if isinstance(first, ast.Call) and _called_name(first.func) in _DESCRIPTOR_SOURCES:
                continue
            offenders.append(f"{module.name}:{node.lineno} {name}({ast.unparse(first)})")
    assert offenders == []


@pytest.mark.parametrize(
    "owner",
    [
        CONFINED,
        GUARDED,
        DECLARED,
        DESCRIPTOR,
        CHILD,
        CANDIDATE_CHILD,
        SOURCE_READ,
        PROCESS_OBSERVATION,
        PROCESS_SIGNAL,
        TEMP_ENVIRONMENT,
        OWN_IMAGE,
    ],
)
def test_the_ownership_document_explains_each_owner_class(owner: str) -> None:
    assert OWNERSHIP_DOC.is_file()
    assert owner in OWNERSHIP_DOC.read_text(encoding="utf-8")


def test_the_ownership_document_names_every_audited_module() -> None:
    text = OWNERSHIP_DOC.read_text(encoding="utf-8")
    modules = {module for module, _function, _access, _owner in OWNERSHIP}
    # These execute no filesystem access of their own; the document says so.
    without_access = {"__init__.py", "models.py"}
    audited_modules = modules | without_access
    assert audited_modules == {path.name for path in _evaluator_sources()}
    for module in sorted(audited_modules):
        assert module in text
