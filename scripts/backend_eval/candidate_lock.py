"""Freeze the ty and pyrefly candidate resolution exactly once, outside production.

The candidate lock is compiled with one explicit ``uv pip compile`` invocation
whose input and output live below the caller's ignored artifact root.  The
resolution is hash-locked, binary-only, non-pre-release, and time-bounded by
``--exclude-newer``; the parser refuses anything the freeze cannot reproduce
(missing hashes, editable or direct-URL requirements, environment markers,
duplicates, pre-release or local versions) and the production identity is
asserted byte-identical around the whole operation.

Subprocess execution is injected through :class:`CommandRunner`; this module has
no other process seam.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scripts.backend_eval.models import (
    CandidateLock,
    CandidatePackage,
    ResolvedPackage,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.production_identity import (
    ProductionIdentityChanged,
    assert_production_identity_unchanged,
    capture_production_identity,
)

__all__ = [
    "CACHE_DIR_NAME",
    "CANDIDATE_NAMES",
    "LOCK_FILE_NAME",
    "RECEIPT_FILE_NAME",
    "REQUIREMENTS_IN_NAME",
    "CandidateLockError",
    "CandidateLockRequest",
    "CommandResult",
    "CommandRunner",
    "ProductionIdentityChanged",
    "compile_candidate_lock",
    "subprocess_runner",
]

# Canonical sorted candidate order; the models require sorted, unique names.
CANDIDATE_NAMES = ("pyrefly", "ty")
REQUIREMENTS_IN_NAME = "candidate-requirements.in"
LOCK_FILE_NAME = "candidate-requirements.lock"
RECEIPT_FILE_NAME = "candidate-lock-receipt.json"
CACHE_DIR_NAME = "uv-cache"

_EXCLUDE_NEWER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$")
# Final PEP 440 releases only: no pre-release, developmental, or local segment.
_FINAL_VERSION_RE = re.compile(r"^(?:\d+!)?\d+(?:\.\d+)*(?:\.post\d+)?$")
_HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
_NAME_SEPARATOR_RE = re.compile(r"[-_.]+")


class CandidateLockError(RuntimeError):
    """Raised when the candidate resolution cannot be frozen reproducibly."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult: ...


def subprocess_runner(command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
    # Explicit absolute argv, never a shell string.
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env),
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


@dataclass(frozen=True, slots=True)
class CandidateLockRequest:
    repo_root: Path
    artifact_root: Path
    uv: Path
    python: Path
    exclude_newer: str

    def __post_init__(self) -> None:
        _validate_directory(self.repo_root, "CandidateLockRequest.repo_root")
        _validate_artifact_root(self.artifact_root, self.repo_root)
        _validate_executable(self.uv, "CandidateLockRequest.uv")
        _validate_executable(self.python, "CandidateLockRequest.python")
        if _EXCLUDE_NEWER_RE.fullmatch(self.exclude_newer) is None:
            raise ValueError(
                "CandidateLockRequest.exclude_newer must be a UTC timestamp such as 2026-08-11T00:00:00Z"
            )


def compile_candidate_lock(
    request: CandidateLockRequest,
    *,
    runner: CommandRunner = subprocess_runner,
    recompile: bool = False,
) -> CandidateLock:
    """Return the frozen candidate lock, resolving at most once per artifact root.

    An existing lock is accepted without a second resolution when recompilation
    is not requested and its canonical receipt matches this request exactly.  A
    requested recompilation must reproduce the frozen bytes exactly.
    """

    before = capture_production_identity(request.repo_root)
    artifact_root = request.artifact_root
    input_path = artifact_root / REQUIREMENTS_IN_NAME
    lock_path = artifact_root / LOCK_FILE_NAME
    receipt_path = artifact_root / RECEIPT_FILE_NAME
    cache_dir = artifact_root / CACHE_DIR_NAME
    command = _compile_command(request)
    frozen_bytes = lock_path.read_bytes() if lock_path.is_file() else None

    if frozen_bytes is not None and not recompile:
        lock = _accept_frozen_lock(request, command, input_path, receipt_path, frozen_bytes)
    else:
        cache_dir.mkdir(parents=True, exist_ok=True)
        requirements_bytes = _write_requirements_input(input_path)
        result = runner(command, cwd=artifact_root, env=_command_env(cache_dir))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CandidateLockError(f"candidate resolution failed ({result.returncode}): {detail}")
        if not lock_path.is_file():
            raise CandidateLockError(f"candidate resolution did not write {lock_path}")
        lock_bytes = lock_path.read_bytes()
        if frozen_bytes is not None and lock_bytes != frozen_bytes:
            lock_path.write_bytes(frozen_bytes)
            raise CandidateLockError(f"candidate resolution changed after the freeze recorded in {lock_path}")
        lock = _build_lock(request, lock_bytes)
        receipt_path.write_bytes(_receipt_bytes(command, requirements_bytes, lock))

    assert_production_identity_unchanged(before, capture_production_identity(request.repo_root))
    return lock


# --- command and environment --------------------------------------------------


def _compile_command(request: CandidateLockRequest) -> tuple[str, ...]:
    return (
        str(request.uv),
        "pip",
        "compile",
        str(request.artifact_root / REQUIREMENTS_IN_NAME),
        "--output-file",
        str(request.artifact_root / LOCK_FILE_NAME),
        "--generate-hashes",
        "--no-annotate",
        "--no-header",
        "--resolution",
        "highest",
        "--prerelease",
        "disallow",
        "--only-binary",
        ":all:",
        "--python",
        str(request.python),
        "--no-sources",
        "--no-python-downloads",
        "--exclude-newer",
        request.exclude_newer,
    )


def _command_env(cache_dir: Path) -> dict[str, str]:
    """Inherit bootstrap's ambient proxy behaviour and add a service-owned cache."""

    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(cache_dir)
    return env


# --- artifact-root inputs and receipts ----------------------------------------


def _write_requirements_input(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{name}\n" for name in CANDIDATE_NAMES), encoding="utf-8")
    return _read_requirements_input(path)


def _read_requirements_input(path: Path) -> bytes:
    if not path.is_file():
        raise CandidateLockError(f"frozen candidate lock is missing its requirements input: {path}")
    data = path.read_bytes()
    names = data.decode("utf-8").split()
    if sorted(names) != list(CANDIDATE_NAMES):
        raise CandidateLockError(
            f"candidate requirements input must list exactly {list(CANDIDATE_NAMES)}, got {names}"
        )
    return data


def _receipt_bytes(command: Sequence[str], requirements_bytes: bytes, lock: CandidateLock) -> bytes:
    return canonical_json(
        {
            "candidate_lock_digest": lock.digest,
            "candidate_names": list(CANDIDATE_NAMES),
            "command": list(command),
            "exclude_newer": lock.exclude_newer,
            "requirements_in_sha256": sha256_bytes(requirements_bytes),
        }
    )


def _accept_frozen_lock(
    request: CandidateLockRequest,
    command: Sequence[str],
    input_path: Path,
    receipt_path: Path,
    frozen_bytes: bytes,
) -> CandidateLock:
    requirements_bytes = _read_requirements_input(input_path)
    if not receipt_path.is_file():
        raise CandidateLockError(f"frozen candidate lock is missing its canonical receipt: {receipt_path}")
    lock = _build_lock(request, frozen_bytes)
    expected = _receipt_bytes(command, requirements_bytes, lock)
    if receipt_path.read_bytes() != expected:
        raise CandidateLockError(f"frozen candidate lock does not match its canonical receipt: {receipt_path}")
    return lock


# --- resolution parsing -------------------------------------------------------


def _build_lock(request: CandidateLockRequest, lock_bytes: bytes) -> CandidateLock:
    resolved_packages = _parse_resolution(lock_bytes)
    resolved_by_name = {package.name: package for package in resolved_packages}
    missing = [name for name in CANDIDATE_NAMES if name not in resolved_by_name]
    if missing:
        raise CandidateLockError(f"candidate resolution is missing required candidates: {', '.join(missing)}")
    candidates = tuple(
        CandidatePackage(
            name=resolved_by_name[name].name,
            version=resolved_by_name[name].version,
            requirement=resolved_by_name[name].requirement,
            artifact_hashes=resolved_by_name[name].artifact_hashes,
            executable_relpath=f"bin/{name}",
        )
        for name in CANDIDATE_NAMES
    )
    return CandidateLock(
        digest=sha256_bytes(lock_bytes),
        exclude_newer=request.exclude_newer,
        resolved_packages=resolved_packages,
        candidates=candidates,
    )


def _parse_resolution(lock_bytes: bytes) -> tuple[ResolvedPackage, ...]:
    try:
        text = lock_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateLockError(f"candidate resolution is not valid UTF-8: {exc}") from exc
    packages: list[ResolvedPackage] = []
    seen: set[str] = set()
    for line in _logical_lines(text):
        if line.startswith("#"):
            continue
        package = _parse_requirement_line(line)
        if package.name in seen:
            raise CandidateLockError(f"candidate resolution contains a duplicate package: {package.name}")
        seen.add(package.name)
        packages.append(package)
    if not packages:
        raise CandidateLockError("candidate resolution is empty")
    return tuple(sorted(packages, key=lambda package: package.name))


def _parse_requirement_line(line: str) -> ResolvedPackage:
    if ";" in line:
        raise CandidateLockError(f"candidate resolution rejects an environment marker: {line}")
    if line.startswith(("-e ", "--editable")):
        raise CandidateLockError(f"candidate resolution rejects an editable requirement: {line}")
    tokens = line.split()
    requirement = tokens[0]
    if requirement.startswith("-"):
        raise CandidateLockError(f"candidate resolution rejects an unexpected requirement option: {line}")
    if "@" in requirement or "://" in line or requirement.startswith(("http", "file:")):
        raise CandidateLockError(f"candidate resolution rejects a direct URL requirement: {line}")
    match = _REQUIREMENT_RE.fullmatch(requirement)
    if match is None:
        raise CandidateLockError(f"candidate resolution requires an exact == pin: {line}")
    name = _NAME_SEPARATOR_RE.sub("-", match.group(1)).lower()
    version = match.group(2)
    if _FINAL_VERSION_RE.fullmatch(version) is None:
        raise CandidateLockError(
            f"candidate resolution rejects a pre-release, developmental, or local version: {requirement}"
        )
    return ResolvedPackage(
        name=name,
        version=version,
        requirement=f"{name}=={version}",
        artifact_hashes=_parse_hashes(name, tokens[1:]),
    )


def _parse_hashes(name: str, tokens: Sequence[str]) -> tuple[tuple[str, str], ...]:
    digests: list[str] = []
    for token in tokens:
        match = _HASH_RE.fullmatch(token)
        if match is None:
            raise CandidateLockError(
                f"candidate resolution requires --hash=sha256:<digest> entries for {name}: {token}"
            )
        digest = match.group(1)
        if digest in digests:
            raise CandidateLockError(f"candidate resolution contains a duplicate hash for {name}: {digest}")
        digests.append(digest)
    if not digests:
        raise CandidateLockError(f"candidate resolution is missing artifact hashes for {name}")
    return tuple(sorted((f"sha256:{digest}", digest) for digest in digests))


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.endswith("\\"):
            buffer += f"{stripped[:-1].strip()} "
            continue
        buffer += stripped
        if buffer:
            lines.append(buffer)
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


# --- request validation -------------------------------------------------------


def _validate_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if not path.is_dir():
        raise ValueError(f"{label} must be an existing directory: {path}")


def _validate_executable(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path, not an ambient executable name")
    if not path.is_file():
        raise ValueError(f"{label} must be an existing file: {path}")


def _validate_artifact_root(artifact_root: Path, repo_root: Path) -> None:
    if not artifact_root.is_absolute():
        raise ValueError("CandidateLockRequest.artifact_root must be an absolute path")
    if artifact_root == repo_root or repo_root.is_relative_to(artifact_root):
        raise ValueError("CandidateLockRequest.artifact_root must not contain the production repository root")
