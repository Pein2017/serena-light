"""Fail-closed workspace identity, location, and edit-root policy.

This module intentionally has no daemon, adapter, or filesystem-inventory
ownership.  Callers supply the current inventory after they build it.  Every
path entering this boundary is resolved before it is classified, so a symlink
cannot turn an apparently in-root operation into an external read or write.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

DATA_ROOT = Path("/data")
CONDA_ENVS_ROOT = Path("/root/miniconda3/envs")
DEFAULT_PYTHON_ENVIRONMENT = "ms"
MS_INTERPRETER = Path("/root/miniconda3/envs/ms/bin/python")
_CONDA_ENVIRONMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class WorkspaceKind(StrEnum):
    """Whether a workspace is Git-owned or an exact read-only directory."""

    GIT = "git"
    NON_GIT_READ_ONLY = "non_git_read_only"


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
    python_environment: str = DEFAULT_PYTHON_ENVIRONMENT
    python_interpreter: Path = MS_INTERPRETER

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or not self.working_subdirectory.is_absolute():
            raise ValueError("workspace identity paths must be absolute")
        if not self.python_environment or not self.python_interpreter.is_absolute():
            raise ValueError("workspace Python environment identity is invalid")
        if not _is_within(self.working_subdirectory, self.root):
            raise ValueError("working subdirectory must be inside the workspace root")

    @property
    def registry_key(self) -> tuple[WorkspaceKind, Path, str, Path]:
        """The key shared by sessions that bind the same physical workspace."""

        return (self.kind, self.root, self.python_environment, self.python_interpreter)


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
class CondaEnvironment:
    """One explicit, validated Conda environment selection."""

    name: str
    interpreter: Path


class CondaEnvironmentResolver:
    """Resolve safe environment names without consulting ambient shell state."""

    def __init__(self, envs_root: Path = CONDA_ENVS_ROOT) -> None:
        self._envs_root = _resolve_existing(envs_root, purpose="Conda environments root")
        if not self._envs_root.is_dir():
            _fail(WorkspaceErrorCode.INVALID_PATH, "Conda environments root must be a directory", path=self._envs_root)

    @property
    def envs_root(self) -> Path:
        return self._envs_root

    def resolve(self, name: str | None = None) -> CondaEnvironment:
        selected = DEFAULT_PYTHON_ENVIRONMENT if name is None else name
        if not isinstance(selected, str) or _CONDA_ENVIRONMENT_NAME.fullmatch(selected) is None:
            _fail(WorkspaceErrorCode.INVALID_PATH, "Python environment name is invalid")
        configured = self._envs_root / selected / "bin" / "python"
        try:
            mode = configured.stat().st_mode
        except OSError:
            _fail(
                WorkspaceErrorCode.INVALID_PATH,
                "selected Conda environment interpreter is unavailable",
                path=configured,
            )
        if not stat.S_ISREG(mode) or not os.access(configured, os.X_OK):
            _fail(
                WorkspaceErrorCode.INVALID_PATH,
                "selected Conda environment interpreter is not executable",
                path=configured,
            )
        # Preserve the configured environment path instead of its symlink target;
        # this value owns Pyright configuration and runtime identity.
        return CondaEnvironment(name=selected, interpreter=configured)

    def environment_for_path(self, path: Path) -> str | None:
        """Classify an already-resolved path below one installed environment."""

        try:
            relative = path.relative_to(self._envs_root)
        except ValueError:
            return None
        if not relative.parts:
            return None
        name = relative.parts[0]
        if _CONDA_ENVIRONMENT_NAME.fullmatch(name) is None:
            return None
        try:
            return self.resolve(name).name
        except WorkspaceError:
            return None


class WorkspacePolicy:
    """Resolve workspace identities and enforce the read/write path boundary."""

    def __init__(
        self,
        *,
        conda_envs_root: Path = CONDA_ENVS_ROOT,
        data_root: Path = DATA_ROOT,
    ) -> None:
        self._environments = CondaEnvironmentResolver(conda_envs_root)
        self._data_root = _resolve_existing(data_root, purpose="data root")

    def resolve_activation(
        self,
        activation_path: str | Path,
        python_environment: str | None = None,
    ) -> WorkspaceIdentity:
        """Validate one absolute activation path and return its physical identity."""

        supplied = Path(activation_path)
        if not supplied.is_absolute():
            _fail(WorkspaceErrorCode.INVALID_PATH, "activation path must be absolute")
        environment = self._environments.resolve(python_environment)
        resolved = _resolve_existing(supplied, purpose="activation path")
        if not resolved.is_dir():
            _fail(WorkspaceErrorCode.INVALID_PATH, "activation path must be a directory", path=resolved)
        git_root = _git_top_level(resolved)
        if git_root is not None:
            return WorkspaceIdentity(
                root=git_root,
                kind=WorkspaceKind.GIT,
                working_subdirectory=resolved,
                python_environment=environment.name,
                python_interpreter=environment.interpreter,
            )
        return WorkspaceIdentity(
            root=resolved,
            kind=WorkspaceKind.NON_GIT_READ_ONLY,
            working_subdirectory=resolved,
            python_environment=environment.name,
            python_interpreter=environment.interpreter,
        )

    def environment_for_path(self, path: Path) -> str | None:
        """Return the evident installed environment for one resolved path."""

        return self._environments.environment_for_path(path)

    def classify_semantic_location(self, identity: WorkspaceIdentity, path: str | Path) -> SemanticLocation:
        """Classify an LSP-returned location without changing workspace inventory."""

        resolved = _resolve_existing(Path(path), purpose="semantic location")
        if _is_within(resolved, identity.root):
            return SemanticLocation(resolved, LocationKind.WORKSPACE)
        return SemanticLocation(resolved, LocationKind.READ_ONLY_EXTERNAL)

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
