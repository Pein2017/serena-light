"""Reproducible identity for one Serena Light daemon build."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol

BUILD_IDENTITY_ALGORITHM_VERSION = 3
# Bump for binding-scoped Python environment selection and flexible non-Git roots.
PUBLIC_TOOL_SCHEMA_VERSION = "5"
RUNTIME_SOURCE_SUFFIXES = frozenset({".mjs", ".py"})
# The dependency slot is content-addressed by resolved lock state, not by
# unrelated project metadata or developer-tool configuration.  Both lockfiles
# include their root project's declared dependency set.
LOCK_INPUTS = ("uv.lock", "package-lock.json")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_source_files(root: Path) -> tuple[Path, ...]:
    """Return the explicit, sorted packaged runtime-source closure."""

    source_root = root / "src" / "serena_light"
    files = tuple(
        sorted(
            (
                path
                for path in source_root.rglob("*")
                if path.is_file() and path.suffix in RUNTIME_SOURCE_SUFFIXES
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )
    if not files:
        raise ValueError(f"no Serena Light runtime sources below {source_root}")
    return files


def dependency_lock_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in LOCK_INPUTS:
        path = root / name
        if not path.is_file():
            raise ValueError(f"missing lock input: {path}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def compute_build_identity(
    root: Path | None = None,
    *,
    public_tool_schema_version: str = PUBLIC_TOOL_SCHEMA_VERSION,
    algorithm_version: int = BUILD_IDENTITY_ALGORITHM_VERSION,
) -> str:
    """Hash source path+bytes, dependency lock digest, and public schema identity."""

    repository = repository_root() if root is None else root.resolve()
    if algorithm_version <= 0:
        raise ValueError("build identity algorithm version must be positive")
    if not public_tool_schema_version:
        raise ValueError("public tool schema version must be non-empty")

    digest = hashlib.sha256()
    _update_field(digest, b"algorithm", str(algorithm_version).encode("ascii"))
    for path in runtime_source_files(repository):
        relative = path.relative_to(repository).as_posix().encode("utf-8")
        _update_field(digest, b"source-path", relative)
        _update_field(digest, b"source-bytes", path.read_bytes())
    _update_field(digest, b"dependency-lock", dependency_lock_digest(repository).encode("ascii"))
    _update_field(digest, b"public-tool-schema", public_tool_schema_version.encode("utf-8"))
    return digest.hexdigest()


def validate_build_identity(value: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise ValueError("build identity must be a lowercase SHA-256 digest")
    return value


def _update_field(digest: _Digest, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)
