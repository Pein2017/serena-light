"""Fail-closed workspace identity, location, and edit-root policy.

This module intentionally has no daemon, adapter, or filesystem-inventory
ownership.  Callers supply the current inventory after they build it.  Every
path entering this boundary is resolved before it is classified, so a symlink
cannot turn an apparently in-root operation into an external read or write.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

DATA_ROOT = Path("/data")
MS_INTERPRETER = Path("/root/miniconda3/envs/ms/bin/python")
TRANSFORMERS_ROOT = Path("/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers")


class WorkspaceKind(StrEnum):
    """The two deliberately narrow v1 workspace identity kinds."""

    GIT = "git"
    ALLOWLISTED_NON_GIT = "allowlisted_non_git"


class LocationKind(StrEnum):
    """Whether a returned semantic location is editable in this binding."""

    WORKSPACE = "workspace"
    READ_ONLY_EXTERNAL = "read_only_external"


class WorkspaceErrorCode(StrEnum):
    INVALID_PATH = "INVALID_PATH"
    UNTRUSTED_ROOT = "UNTRUSTED_ROOT"
    OUT_OF_WORKSPACE = "OUT_OF_WORKSPACE"
    READ_ONLY_ROOT = "READ_ONLY_ROOT"


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """A stable registry key plus connection-local working-directory metadata."""

    root: Path
    kind: WorkspaceKind
    working_subdirectory: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or not self.working_subdirectory.is_absolute():
            raise ValueError("workspace identity paths must be absolute")
        if not _is_within(self.working_subdirectory, self.root):
            raise ValueError("working subdirectory must be inside the workspace root")

    @property
    def registry_key(self) -> tuple[WorkspaceKind, Path]:
        """The key shared by sessions that bind the same physical workspace."""

        return (self.kind, self.root)


@dataclass(frozen=True, slots=True)
class SemanticLocation:
    path: Path
    kind: LocationKind

    @property
    def read_only_external(self) -> bool:
        return self.kind is LocationKind.READ_ONLY_EXTERNAL


@dataclass(frozen=True, slots=True)
class WorkspaceErrorData:
    code: WorkspaceErrorCode
    message: str
    current_identity: WorkspaceIdentity | None = None
    activation_hint: Path | None = None
    path: Path | None = None


class WorkspaceError(ValueError):
    """Typed, transport-neutral failure data for later JSON envelope owners."""

    def __init__(self, data: WorkspaceErrorData) -> None:
        super().__init__(data.message)
        self.data = data


@dataclass(frozen=True, slots=True)
class PinnedMsRoots:
    """Resolved roots reported by the fixed conda ``ms`` interpreter."""

    interpreter: Path
    stdlib: Path
    purelib: Path
    platlib: Path
    conda_prefix: Path

    @classmethod
    def resolve(cls, interpreter: Path = MS_INTERPRETER) -> PinnedMsRoots:
        """Query ``sysconfig`` through the pinned interpreter, never ambient Python."""

        resolved_interpreter = _resolve_existing(interpreter, purpose="pinned ms interpreter")
        if not resolved_interpreter.is_file():
            _fail(WorkspaceErrorCode.INVALID_PATH, "pinned ms interpreter is not a file", path=resolved_interpreter)
        program = (
            "import json, sys, sysconfig; "
            "print(json.dumps({'stdlib': sysconfig.get_path('stdlib'), "
            "'purelib': sysconfig.get_path('purelib'), 'platlib': sysconfig.get_path('platlib'), "
            "'prefix': sys.prefix}))"
        )
        try:
            completed = subprocess.run(
                [str(resolved_interpreter), "-c", program],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            reported = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            _fail(
                WorkspaceErrorCode.INVALID_PATH,
                f"could not resolve pinned ms roots: {error}",
                path=resolved_interpreter,
            )
        if not isinstance(reported, dict):  # pragma: no cover - defensive for hostile interpreter output
            _fail(WorkspaceErrorCode.INVALID_PATH, "pinned ms interpreter returned invalid sysconfig data")
        roots: dict[str, Path] = {}
        for name in ("stdlib", "purelib", "platlib", "prefix"):
            value = reported.get(name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                _fail(WorkspaceErrorCode.INVALID_PATH, f"pinned ms interpreter reported invalid {name}")
            roots[name] = _resolve_existing(Path(value), purpose=f"pinned ms {name}")
        return cls(
            interpreter=resolved_interpreter,
            stdlib=roots["stdlib"],
            purelib=roots["purelib"],
            platlib=roots["platlib"],
            conda_prefix=roots["prefix"],
        )

    @property
    def semantic_roots(self) -> tuple[Path, Path, Path]:
        return (self.stdlib, self.purelib, self.platlib)

    def contains_semantic_path(self, path: Path) -> bool:
        return any(_is_within(path, root) for root in self.semantic_roots)

    def contains_conda_path(self, path: Path) -> bool:
        return _is_within(path, self.conda_prefix)


class WorkspacePolicy:
    """Resolve only v1 workspace identities and enforce their path boundary."""

    def __init__(
        self,
        *,
        ms_roots: PinnedMsRoots,
        allowed_non_git_root: Path = TRANSFORMERS_ROOT,
        data_root: Path = DATA_ROOT,
    ) -> None:
        self._ms_roots = ms_roots
        self._allowed_non_git_root = _resolve_existing(allowed_non_git_root, purpose="allowlisted non-Git root")
        self._data_root = _resolve_existing(data_root, purpose="data root")

    @property
    def allowed_non_git_root(self) -> Path:
        return self._allowed_non_git_root

    def resolve_activation(self, activation_path: str | Path) -> WorkspaceIdentity:
        """Validate one absolute activation path and return its physical identity."""

        supplied = Path(activation_path)
        if not supplied.is_absolute():
            _fail(WorkspaceErrorCode.INVALID_PATH, "activation path must be absolute")
        resolved = _resolve_existing(supplied, purpose="activation path")
        if not resolved.is_dir():
            _fail(WorkspaceErrorCode.INVALID_PATH, "activation path must be a directory", path=resolved)
        git_root = _git_top_level(resolved)
        if git_root is not None:
            return WorkspaceIdentity(root=git_root, kind=WorkspaceKind.GIT, working_subdirectory=resolved)
        if resolved == self._allowed_non_git_root:
            return WorkspaceIdentity(
                root=resolved,
                kind=WorkspaceKind.ALLOWLISTED_NON_GIT,
                working_subdirectory=resolved,
            )
        _fail(
            WorkspaceErrorCode.UNTRUSTED_ROOT,
            "activation path is not a Git workspace or exact allowlisted root",
            path=resolved,
        )

    def classify_semantic_location(self, identity: WorkspaceIdentity, path: str | Path) -> SemanticLocation:
        """Classify an LSP-returned location without changing workspace inventory."""

        resolved = _resolve_existing(Path(path), purpose="semantic location")
        if _is_within(resolved, identity.root):
            return SemanticLocation(resolved, LocationKind.WORKSPACE)
        if _is_within(resolved, self._data_root) or self._ms_roots.contains_semantic_path(resolved):
            return SemanticLocation(resolved, LocationKind.READ_ONLY_EXTERNAL)
        _fail(WorkspaceErrorCode.UNTRUSTED_ROOT, "semantic location is outside trusted query roots", path=resolved)

    def authorize_path_operand(
        self,
        identity: WorkspaceIdentity,
        path: str | Path,
        inventory: Collection[Path],
    ) -> Path:
        """Require an existing, resolved operand in the caller-owned active inventory."""

        resolved = _resolve_workspace_operand(identity, Path(path), purpose="workspace path operand")
        if not _is_within(resolved, identity.root):
            self._out_of_workspace(identity, resolved)
        if resolved not in _resolved_inventory(inventory):
            self._out_of_workspace(identity, resolved)
        return resolved

    def authorize_edit(self, identity: WorkspaceIdentity, path: str | Path, inventory: Collection[Path]) -> Path:
        """Authorize an edit lexically, refusing any symlinked path component.

        Membership is decided on the lexical path the caller named, never on a
        resolved one: resolution would let a tracked path that has been replaced
        by a symlink to an in-root ignored file inherit that file's identity and
        pass an inventory check.  The component walk below then proves that the
        lexical path is also the physical one, which is what keeps the edit
        core's ``path.as_uri()`` snapshot checks meaningful.
        """

        lexical = _lexical_workspace_path(identity, Path(path))
        if self._ms_roots.contains_conda_path(lexical):
            _fail(WorkspaceErrorCode.READ_ONLY_ROOT, "conda environment paths are read-only", path=lexical)
        if identity.kind is not WorkspaceKind.GIT or not _is_within(identity.root, self._data_root):
            _fail(WorkspaceErrorCode.READ_ONLY_ROOT, "only Git workspaces below /data are editable", path=lexical)
        if not _is_within(lexical, identity.root):
            self._out_of_workspace(identity, lexical)
        if lexical not in _lexical_inventory(inventory):
            self._out_of_workspace(identity, lexical)
        require_guarded_regular_file(identity.root, lexical.relative_to(identity.root).parts)
        return lexical

    def _out_of_workspace(self, identity: WorkspaceIdentity, path: Path) -> None:
        hint = _activation_hint(path)
        _fail(
            WorkspaceErrorCode.OUT_OF_WORKSPACE,
            "path is outside the active workspace inventory",
            current_identity=identity,
            activation_hint=hint,
            path=path,
        )


def open_guarded_directory(root: Path, parts: Sequence[str]) -> int:
    """Open one in-root directory, refusing to traverse any symlinked component.

    The returned descriptor is the caller's to close.  ``root`` is already a
    resolved workspace root, so every component after it is opened with
    ``O_NOFOLLOW`` and ``O_DIRECTORY``: a symlink component fails with ``ELOOP``
    and a non-directory component with ``ENOTDIR`` before anything is read.
    """

    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise OSError(f"path component is not normalized: {part!r}")
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def require_guarded_regular_file(root: Path, parts: Sequence[str]) -> None:
    """Fail closed unless ``root/parts`` is a regular file reached without links."""

    if not parts:
        _fail(WorkspaceErrorCode.INVALID_PATH, "edit target must name a file", path=root)
    try:
        directory_fd = open_guarded_directory(root, parts[:-1])
    except OSError as error:
        _fail(
            WorkspaceErrorCode.INVALID_PATH,
            f"edit target directory is not a guarded in-root path: {error}",
            path=root.joinpath(*parts),
        )
    try:
        try:
            file_stat = os.lstat(parts[-1], dir_fd=directory_fd)
        except OSError as error:
            _fail(
                WorkspaceErrorCode.INVALID_PATH,
                f"edit target does not exist as a guarded in-root path: {error}",
                path=root.joinpath(*parts),
            )
    finally:
        os.close(directory_fd)
    if not stat.S_ISREG(file_stat.st_mode):
        _fail(
            WorkspaceErrorCode.INVALID_PATH,
            "edit target is a symlink or not a regular file",
            path=root.joinpath(*parts),
        )


def _git_top_level(path: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError as error:
        _fail(WorkspaceErrorCode.INVALID_PATH, f"could not execute git: {error}", path=path)
    if completed.returncode != 0:
        return None
    candidate = completed.stdout.strip()
    if not candidate:
        _fail(WorkspaceErrorCode.INVALID_PATH, "git returned an empty top-level path", path=path)
    return _resolve_existing(Path(candidate), purpose="Git top-level")


def _activation_hint(path: Path) -> Path:
    """Give callers an absolute directory they can pass to activate_workspace."""

    return path if path.is_dir() else path.parent


def _resolve_workspace_operand(identity: WorkspaceIdentity, path: Path, *, purpose: str) -> Path:
    """Resolve tool ``relative_path`` values from the workspace identity root."""

    candidate = path if path.is_absolute() else identity.root / path
    return _resolve_existing(candidate, purpose=purpose)


def _lexical_workspace_path(identity: WorkspaceIdentity, path: Path) -> Path:
    """Interpret one operand against the workspace root without following links.

    ``..`` is collapsed lexically rather than by the filesystem, so a parent
    reference names the directory it spells and an escape stays an escape.  The
    guarded component walk later proves the collapsed path is also the physical
    one, so this never authorizes a link-relative target.
    """

    candidate = path if path.is_absolute() else identity.root / path
    if "\x00" in str(candidate):
        _fail(WorkspaceErrorCode.INVALID_PATH, "edit target must not contain a null byte", path=candidate)
    return Path(os.path.normpath(candidate))


def _lexical_inventory(inventory: Collection[Path]) -> frozenset[Path]:
    """Compare inventory membership exactly as the inventory owner named it."""

    return frozenset(Path(os.path.normpath(item)) for item in inventory)


def _resolved_inventory(inventory: Collection[Path]) -> frozenset[Path]:
    """Inventory generation owns filtering; this boundary only normalizes comparison."""

    resolved: set[Path] = set()
    for item in inventory:
        try:
            resolved.add(item.resolve(strict=True))
        except OSError:
            # Stale inventory entries are never authorization grants.
            continue
    return frozenset(resolved)


def _resolve_existing(path: Path, *, purpose: str) -> Path:
    if not path.is_absolute():
        _fail(WorkspaceErrorCode.INVALID_PATH, f"{purpose} must be absolute", path=path)
    try:
        return path.resolve(strict=True)
    except OSError as error:
        _fail(WorkspaceErrorCode.INVALID_PATH, f"{purpose} does not resolve: {error}", path=path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fail(
    code: WorkspaceErrorCode,
    message: str,
    *,
    current_identity: WorkspaceIdentity | None = None,
    activation_hint: Path | None = None,
    path: Path | None = None,
) -> NoReturn:
    raise WorkspaceError(WorkspaceErrorData(code, message, current_identity, activation_hint, path))
