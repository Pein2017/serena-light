"""Deterministic, Git-bounded source inventories for workspace trust.

The inventory deliberately answers an authorization question, not an LSP project
membership question.  Git is asked for candidates, so ignored directories are
never walked.  Every candidate is then checked by ``lstat`` and a strict
resolution check before it can enter the supported-language index.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
JAVASCRIPT_TYPESCRIPT_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
SUPPORTED_EXTENSIONS = PYTHON_EXTENSIONS | JAVASCRIPT_TYPESCRIPT_EXTENSIONS

_PRUNED_DIRECTORY_NAMES = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules"}
)


@dataclass(frozen=True, slots=True)
class RejectedPath:
    """A candidate excluded from trust with a stable, inspectable reason."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class TargetedPathState:
    """One bounded stat observation suitable for freshness comparison."""

    path: str
    trusted: bool
    size: int | None
    mtime_ns: int | None
    reason: str | None


class SupportedPathTree:
    """A compact prefix index over normalized, supported relative paths."""

    __slots__ = ("_children", "_terminal")

    def __init__(self, paths: Iterable[str] = ()) -> None:
        self._children: dict[str, SupportedPathTree] = {}
        self._terminal = False
        for path in paths:
            self.add(path)

    def add(self, path: str) -> None:
        parts = _normalized_parts(path)
        node = self
        for part in parts:
            node = node._children.setdefault(part, SupportedPathTree())
        node._terminal = True

    def contains(self, path: str | Path) -> bool:
        try:
            parts = _normalized_parts(path)
        except ValueError:
            return False
        node = self
        for part in parts:
            node = node._children.get(part)
            if node is None:
                return False
        return node._terminal

    def has_prefix(self, path: str | Path) -> bool:
        """Whether a supported path is equal to or below ``path``."""
        try:
            parts = _normalized_parts(path)
        except ValueError:
            return False
        node = self
        for part in parts:
            node = node._children.get(part)
            if node is None:
                return False
        return node._terminal or bool(node._children)

    def iter_prefix(self, path: str | Path = ".") -> Iterator[str]:
        """Yield indexed files under a normalized prefix in lexical order."""
        parts = () if str(path) in {"", "."} else _normalized_parts(path)
        node = self
        for part in parts:
            node = node._children.get(part)
            if node is None:
                return
        yield from self._walk(parts, node)

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> SupportedPathTree:
        return cls(paths)

    def _walk(self, prefix: tuple[str, ...], node: SupportedPathTree) -> Iterator[str]:
        if node._terminal:
            yield "/".join(prefix)
        for name in sorted(node._children):
            yield from self._walk((*prefix, name), node._children[name])


@dataclass(frozen=True, slots=True)
class TrustInventory:
    """One immutable trust snapshot and its query index."""

    root: Path
    paths: tuple[str, ...]
    rejected: tuple[RejectedPath, ...]
    digest: str
    tree: SupportedPathTree
    kind: str

    @property
    def count(self) -> int:
        return len(self.paths)

    @property
    def absolute_paths(self) -> tuple[Path, ...]:
        """Materialize the normalized authorization paths for policy checks."""

        return tuple(self.root / relative for relative in self.paths)

    def contains(self, relative_path: str | Path) -> bool:
        return self.tree.contains(relative_path)

    def paths_under(self, relative_path: str | Path = ".") -> tuple[str, ...]:
        return tuple(self.tree.iter_prefix(relative_path))

    def targeted_freshness(self, paths: Iterable[str | Path]) -> tuple[RejectedPath, ...]:
        """Stat only supplied relative paths; never discover sibling packages.

        Callers use this after a watcher event.  A returned entry records why a
        formerly trusted or newly proposed path cannot currently be trusted.
        """
        rejected: list[RejectedPath] = []
        for raw_path in paths:
            try:
                relative = _normalize_relative(raw_path)
            except ValueError:
                rejected.append(RejectedPath(str(raw_path), "invalid_relative_path"))
                continue
            reason = _candidate_reason(self.root, relative)
            if reason is not None:
                rejected.append(RejectedPath(relative, reason))
        return tuple(sorted(set(rejected), key=lambda item: (item.path, item.reason)))

    def targeted_states(self, paths: Iterable[str | Path]) -> tuple[TargetedPathState, ...]:
        """Return deterministic stat facts for only the caller-named paths."""

        states: set[TargetedPathState] = set()
        for raw_path in paths:
            try:
                relative = _normalize_relative(raw_path)
            except ValueError:
                states.add(TargetedPathState(str(raw_path), False, None, None, "invalid_relative_path"))
                continue
            reason, file_stat = _inspect_candidate(self.root, relative)
            if reason is not None:
                states.add(TargetedPathState(relative, False, None, None, reason))
                continue
            assert file_stat is not None
            states.add(TargetedPathState(relative, True, file_stat.st_size, file_stat.st_mtime_ns, None))
        return tuple(sorted(states, key=lambda item: item.path))


def git_trust_inventory(root: Path) -> TrustInventory:
    """Build the Git cached-plus-untracked inventory without filesystem walking."""
    resolved_root = root.resolve(strict=True)
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=resolved_root,
        check=True,
        capture_output=True,
    )
    candidates = (_decode_git_path(item) for item in completed.stdout.split(b"\0") if item)
    return _inventory_from_candidates(resolved_root, candidates, kind="git")


def discover_transformers_root(site_package_roots: Iterable[Path]) -> Path | None:
    """Find only the exact ``transformers`` child of explicit site-package roots.

    This intentionally does not glob or walk site-packages: callers provide the
    purelib/platlib roots resolved from the pinned interpreter at startup.
    """
    for parent in site_package_roots:
        try:
            parent_resolved = parent.resolve(strict=True)
            candidate = parent_resolved / "transformers"
            mode = candidate.lstat().st_mode
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode) and resolved == candidate:
            return candidate
    return None


def transformers_trust_inventory(root: Path) -> TrustInventory:
    """Boundedly index the exact trusted package root, without following links."""
    resolved_root = _non_symlink_directory(root)
    return _inventory_from_candidates(resolved_root, _bounded_candidates(resolved_root), kind="bounded_no_symlink")


def _inventory_from_candidates(root: Path, candidates: Iterable[str], *, kind: str) -> TrustInventory:
    accepted: set[str] = set()
    rejected: set[RejectedPath] = set()
    for raw in candidates:
        try:
            relative = _normalize_relative(raw)
        except ValueError:
            rejected.add(RejectedPath(str(raw), "invalid_relative_path"))
            continue
        if Path(relative).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        reason = _candidate_reason(root, relative)
        if reason is None:
            accepted.add(relative)
        else:
            rejected.add(RejectedPath(relative, reason))
    paths = tuple(sorted(accepted))
    return TrustInventory(
        root=root,
        paths=paths,
        rejected=tuple(sorted(rejected, key=lambda item: (item.path, item.reason))),
        digest=_path_digest(paths),
        tree=SupportedPathTree(paths),
        kind=kind,
    )


def _bounded_candidates(root: Path) -> Iterator[str]:
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        names[:] = sorted(
            name
            for name in names
            if name not in _PRUNED_DIRECTORY_NAMES and not name.startswith(".") and not (base / name).is_symlink()
        )
        for name in sorted(files):
            yield (base / name).relative_to(root).as_posix()


def _candidate_reason(root: Path, relative: str) -> str | None:
    return _inspect_candidate(root, relative)[0]


def _inspect_candidate(root: Path, relative: str) -> tuple[str | None, os.stat_result | None]:
    lexical = root / relative
    try:
        file_stat = lexical.lstat()
    except FileNotFoundError:
        return "missing", None
    except (OSError, RuntimeError):
        return "unreadable", None
    if stat.S_ISLNK(file_stat.st_mode):
        try:
            resolved = lexical.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            return "symlink", None
        return ("symlink_escape" if not resolved.is_relative_to(root) else "symlink"), None
    if not stat.S_ISREG(file_stat.st_mode):
        return "non_regular", None
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return "missing", None
    if not resolved.is_relative_to(root):
        return "symlink_escape", None
    return None, file_stat


def _non_symlink_directory(path: Path) -> Path:
    """Resolve one explicitly named directory while refusing a link at its root."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError(f"trusted package root must be a non-symlink directory: {path}")
    return path.resolve(strict=True)


def _decode_git_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape").replace("\\", "/")


def _normalize_relative(path: str | Path) -> str:
    return "/".join(_normalized_parts(path))


def _normalized_parts(path: str | Path) -> tuple[str, ...]:
    text = str(path).replace("\\", "/")
    if text.startswith("/"):
        raise ValueError("path must be relative")
    parts = tuple(part for part in text.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ValueError("path must be a non-empty normalized relative path")
    return parts


def _path_digest(paths: Iterable[str]) -> str:
    return hashlib.sha256("\0".join(paths).encode("utf-8", "surrogateescape")).hexdigest()
