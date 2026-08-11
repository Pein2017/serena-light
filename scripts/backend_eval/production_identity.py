"""Capture the production identity the evaluation may never change.

The evaluation locks and installs candidate backends outside the production
dependency slot.  Every phase brackets its work with :func:`capture_production_identity`
and :func:`assert_production_identity_unchanged` so that a candidate resolution,
installation, probe, or cleanup that touched ``pyproject.toml``, ``uv.lock``,
``package-lock.json``, the dependency-lock digest, the build identity, or the
production runtime paths fails loudly instead of silently mutating the installed
service.
"""

from __future__ import annotations

from pathlib import Path

from scripts.backend_eval.models import ProductionIdentity, sha256_bytes
from serena_light.bootstrap import BootstrapError, runtime_paths
from serena_light.build_identity import compute_build_identity, dependency_lock_digest

PRODUCTION_IDENTITY_FILES = ("package-lock.json", "pyproject.toml", "uv.lock")

_IDENTITY_LABELS = (
    ("pyproject_toml_sha256", "pyproject.toml"),
    ("uv_lock_sha256", "uv.lock"),
    ("package_lock_json_sha256", "package-lock.json"),
    ("dependency_lock_digest", "dependency_lock_digest"),
    ("build_identity", "build_identity"),
    ("runtime_paths", "runtime_paths"),
)


class ProductionIdentityError(RuntimeError):
    """Raised when the production identity cannot be captured exactly."""


class ProductionIdentityChanged(ProductionIdentityError):
    """Raised when any production identity field changed across an evaluation step."""


def capture_production_identity(repo_root: Path) -> ProductionIdentity:
    """Return the byte-exact production identity of ``repo_root``.

    The dependency-lock digest, build identity, and runtime paths come from the
    production implementations; only the per-file digests are computed here.
    """

    root = repo_root.resolve()
    digests = {name: sha256_bytes(_read_guarded(root, name)) for name in PRODUCTION_IDENTITY_FILES}
    try:
        lock_digest = dependency_lock_digest(root)
        build_identity = compute_build_identity(root)
        paths = runtime_paths(root)
    except (BootstrapError, ValueError) as exc:
        raise ProductionIdentityError(f"cannot capture production identity below {root}: {exc}") from exc
    return ProductionIdentity(
        pyproject_toml_sha256=digests["pyproject.toml"],
        uv_lock_sha256=digests["uv.lock"],
        package_lock_json_sha256=digests["package-lock.json"],
        dependency_lock_digest=lock_digest,
        build_identity=build_identity,
        runtime_paths=tuple(sorted((name, str(path)) for name, path in paths.items())),
    )


def assert_production_identity_unchanged(before: ProductionIdentity, after: ProductionIdentity) -> None:
    """Raise when any production lock, digest, identity, or runtime path changed."""

    changed = [
        label
        for field_name, label in _IDENTITY_LABELS
        if getattr(before, field_name) != getattr(after, field_name)
    ]
    if changed:
        raise ProductionIdentityChanged(f"production identity changed: {', '.join(changed)}")


def _read_guarded(root: Path, name: str) -> bytes:
    path = root / name
    if path.is_symlink():
        raise ProductionIdentityError(f"production identity input must be a regular file, not a symlink: {path}")
    if not path.is_file():
        raise ProductionIdentityError(f"missing production identity input: {path}")
    return path.read_bytes()
