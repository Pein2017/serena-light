"""Canonical evaluation models and serialization for the backend evaluation harness.

Every later task consumes these exact frozen dataclasses and the two module
functions below; no later task may introduce a second receipt or manifest
representation.

The models are *structurally* strict rather than descriptive.  A
:class:`RootManifest` recomputes its own ``manifest_digest`` from its canonical fields at
construction and at parsing, so forged counts or records with a retained digest fail; a
:class:`CandidateLock` carries an explicit raw-lock digest witness rather than pretending
its structured fields recompute a raw file's bytes; and a ``pass``
:class:`AdmissionReceipt` must carry the complete Phase 1 evidence set -- evaluator, host,
bootstrap environment, candidate runtime binding, both corpus sides, and one delta per
root -- with no issue, no unexpected path, no declared mutation, and no changed manifest
control.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    # Deferred to a function-local import at every runtime use site: the admission CLI's
    # closure test (test_source_binding.py) asserts its non-stdlib module set is exactly
    # {"scripts", "serena_light"}, and a module-level import here would pull `psutil` (via
    # serena_light.lsp.adapter -> serena_light.processes) into that closure even though
    # admission.py never constructs a Phase 2 protocol record.
    from serena_light.lsp.adapter import RawLspProviders

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$")
_ENVIRONMENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_RECORD_KINDS = frozenset({"directory", "file", "special", "symlink"})
_PATH_RECORD_DISPOSITIONS = frozenset({"tracked", "untracked", "ignored", "declared"})
_ROOT_MANIFEST_KINDS = frozenset({"git", "non_git"})
_ADMISSION_STATUSES = frozenset({"pass", "hold", "incomplete", "fail"})
_CANDIDATE_NAMES = frozenset({"ty", "pyrefly"})
_SERVICE_CONFIG_BACKENDS = frozenset({"pyright", "ty", "pyrefly"})
_REQUIRED_ENVIRONMENT_NAMES = frozenset({"ms", "llm-framework-study"})

# Schema 2 adds the evaluator/host, bootstrap-environment, and candidate-runtime bindings,
# the immutable per-execution run identity, and the two-stage corpus remainder evidence.
ADMISSION_RECEIPT_SCHEMA_VERSION = 2
EVALUATION_CONTRACT_VERSION = "python-backend-evaluation-v1"
NEXT_ACTION_PASS = "begin_protocol_probe_planning"
NEXT_ACTION_HOLD = "retain_pyright_and_disposition_admission"


def canonical_json(value: Mapping[str, object]) -> bytes:
    """Serialize a mapping to sorted, compact, UTF-8, newline-terminated JSON bytes."""

    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# --- validation helpers -----------------------------------------------------


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256 digest")
    return value


def _validate_non_empty_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validate_absolute_path(value: object, label: str) -> str:
    text = _validate_non_empty_str(value, label)
    if not text.startswith("/"):
        raise ValueError(f"{label} must be an absolute path")
    return text


def _validate_relative_path(value: object, label: str) -> str:
    text = _validate_non_empty_str(value, label)
    if text.startswith("/"):
        raise ValueError(f"{label} must be a relative path")
    return text


def _validate_utc_timestamp(value: object, label: str) -> str:
    text = _validate_non_empty_str(value, label)
    if _UTC_TIMESTAMP_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a UTC timestamp such as 2026-08-11T00:00:00Z")
    return text


def _validate_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _validate_positive_int(value: object, label: str) -> int:
    return _validate_int(value, label, minimum=1)


def _validate_unique_names(names: Sequence[str], label: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"{label} contains duplicate name: {name!r}")
        seen.add(name)


def _validate_sorted_unique(names: Sequence[str], label: str) -> None:
    _validate_unique_names(names, label)
    if list(names) != sorted(names):
        raise ValueError(f"{label} must be in canonical sorted order")


def _validate_tuple(value: object, label: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{label} must be a tuple, not {type(value).__name__}")
    return value


def _validate_string_tuple(value: object, label: str) -> tuple[str, ...]:
    items = _validate_tuple(value, label)
    for item in items:
        _validate_non_empty_str(item, f"{label} item")
    _validate_sorted_unique(cast("Sequence[str]", items), label)
    return cast("tuple[str, ...]", items)


def _validate_artifact_hashes(value: object, label: str) -> tuple[str, ...]:
    hashes = _validate_tuple(value, label)
    if not hashes:
        raise ValueError(f"{label} must not be empty")
    for digest in hashes:
        _validate_sha256(digest, label)
    _validate_sorted_unique(cast("Sequence[str]", hashes), label)
    return cast("tuple[str, ...]", hashes)


def _validate_path_pairs(value: object, label: str) -> tuple[tuple[str, str], ...]:
    pairs = _validate_tuple(value, label)
    names: list[str] = []
    for entry in pairs:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError(f"{label} entries must be (name, path) tuples")
        name, path = entry
        _validate_non_empty_str(name, f"{label} name")
        _validate_non_empty_str(path, f"{label} path")
        names.append(name)
    _validate_sorted_unique(names, label)
    return pairs


# --- decode helpers ----------------------------------------------------------


def _expect_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast("Mapping[str, Any]", value)


def _expect_list(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _expect_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _expect_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _expect_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _expect_str_list(value: object, label: str) -> tuple[str, ...]:
    return tuple(_expect_str(item, f"{label} item") for item in _expect_list(value, label))


def _expect_pair_list(value: object, label: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in _expect_list(value, label):
        entry = _expect_list(item, f"{label} entry")
        if len(entry) != 2:
            raise ValueError(f"{label} entries must be two-element arrays")
        pairs.append((_expect_str(entry[0], f"{label} key"), _expect_str(entry[1], f"{label} value")))
    return tuple(pairs)


def _closed_fields(value: Mapping[str, object], required: frozenset[str], label: str) -> None:
    missing = required - value.keys()
    if missing:
        raise ValueError(f"{label} is missing required fields: {sorted(missing)}")
    unknown = value.keys() - required
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")


# --- PhaseBudget --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseBudget:
    name: str
    seconds: int

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "PhaseBudget.name")
        _validate_positive_int(self.seconds, "PhaseBudget.seconds")


_PHASE_BUDGET_FIELDS = frozenset({"name", "seconds"})


def _phase_budget_to_dict(budget: PhaseBudget) -> dict[str, object]:
    return {"name": budget.name, "seconds": budget.seconds}


def _phase_budget_from_dict(value: object) -> PhaseBudget:
    mapping = _expect_mapping(value, "PhaseBudget")
    _closed_fields(mapping, _PHASE_BUDGET_FIELDS, "PhaseBudget")
    return PhaseBudget(
        name=_expect_str(mapping["name"], "PhaseBudget.name"),
        seconds=_expect_int(mapping["seconds"], "PhaseBudget.seconds"),
    )


DEFAULT_PHASE_BUDGETS: Mapping[str, PhaseBudget] = MappingProxyType(
    {
        "admission": PhaseBudget("admission", 30 * 60),
        "protocol": PhaseBudget("protocol", 90 * 60),
        "product_seam": PhaseBudget("product_seam", 3 * 60 * 60),
        "feature": PhaseBudget("feature", 2 * 60 * 60),
        "agent": PhaseBudget("agent", 8 * 60 * 60),
        "total": PhaseBudget("total", 16 * 60 * 60),
    }
)


def default_phase_budgets() -> tuple[PhaseBudget, ...]:
    """The frozen Phase 1 budget set in canonical sorted order."""

    return tuple(sorted(DEFAULT_PHASE_BUDGETS.values(), key=lambda budget: budget.name))


# --- ProductionIdentity --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductionIdentity:
    pyproject_toml_sha256: str
    uv_lock_sha256: str
    package_lock_json_sha256: str
    dependency_lock_digest: str
    build_identity: str
    runtime_paths: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _validate_sha256(self.pyproject_toml_sha256, "ProductionIdentity.pyproject_toml_sha256")
        _validate_sha256(self.uv_lock_sha256, "ProductionIdentity.uv_lock_sha256")
        _validate_sha256(self.package_lock_json_sha256, "ProductionIdentity.package_lock_json_sha256")
        _validate_sha256(self.dependency_lock_digest, "ProductionIdentity.dependency_lock_digest")
        _validate_sha256(self.build_identity, "ProductionIdentity.build_identity")
        _validate_path_pairs(self.runtime_paths, "ProductionIdentity.runtime_paths")


_PRODUCTION_IDENTITY_FIELDS = frozenset(
    {
        "pyproject_toml_sha256",
        "uv_lock_sha256",
        "package_lock_json_sha256",
        "dependency_lock_digest",
        "build_identity",
        "runtime_paths",
    }
)


def _production_identity_to_dict(identity: ProductionIdentity) -> dict[str, object]:
    return {
        "pyproject_toml_sha256": identity.pyproject_toml_sha256,
        "uv_lock_sha256": identity.uv_lock_sha256,
        "package_lock_json_sha256": identity.package_lock_json_sha256,
        "dependency_lock_digest": identity.dependency_lock_digest,
        "build_identity": identity.build_identity,
        "runtime_paths": dict(identity.runtime_paths),
    }


def _production_identity_from_dict(value: object) -> ProductionIdentity:
    mapping = _expect_mapping(value, "ProductionIdentity")
    _closed_fields(mapping, _PRODUCTION_IDENTITY_FIELDS, "ProductionIdentity")
    runtime_paths_mapping = _expect_mapping(mapping["runtime_paths"], "ProductionIdentity.runtime_paths")
    runtime_paths = tuple(
        sorted(
            (
                _expect_str(key, "ProductionIdentity.runtime_paths key"),
                _expect_str(path, "ProductionIdentity.runtime_paths value"),
            )
            for key, path in runtime_paths_mapping.items()
        )
    )
    return ProductionIdentity(
        pyproject_toml_sha256=_expect_str(mapping["pyproject_toml_sha256"], "ProductionIdentity.pyproject_toml_sha256"),
        uv_lock_sha256=_expect_str(mapping["uv_lock_sha256"], "ProductionIdentity.uv_lock_sha256"),
        package_lock_json_sha256=_expect_str(
            mapping["package_lock_json_sha256"], "ProductionIdentity.package_lock_json_sha256"
        ),
        dependency_lock_digest=_expect_str(
            mapping["dependency_lock_digest"], "ProductionIdentity.dependency_lock_digest"
        ),
        build_identity=_expect_str(mapping["build_identity"], "ProductionIdentity.build_identity"),
        runtime_paths=runtime_paths,
    )


# --- EvaluatorIdentity ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluatorIdentity:
    """The exact evaluator code and CLI host that produced one receipt.

    ``source_digest`` is recomputed from ``source_files``, so a receipt cannot claim a
    source closure it does not carry.  ``source_commit`` is recorded when the checkout is
    a clean Git tree, as corroboration -- never instead of the executed bytes.

    The evaluator is not only ``scripts/backend_eval``.  Manifests, the write guard, and the
    production-identity capture execute ``serena_light`` helpers, which a CLI host virtual
    environment can resolve into another checkout entirely.  ``production_files`` therefore
    carries the bytes of every executed production helper, ``production_root`` names the
    checkout they were loaded from, and ``production_clean`` records whether that source was
    clean at ``source_commit``.  Both closures feed the evaluation identity, so a changed or
    repointed helper produces different evidence rather than silently different behaviour.
    """

    source_digest: str
    source_files: tuple[tuple[str, str], ...]
    source_commit: str | None
    source_clean: bool
    production_root: str
    production_digest: str
    production_files: tuple[tuple[str, str], ...]
    production_clean: bool
    host_python_path: str
    host_python_realpath: str
    host_python_sha256: str
    host_python_version: str

    def __post_init__(self) -> None:
        _validate_digest_pairs(self.source_files, "EvaluatorIdentity.source_files")
        _validate_sha256(self.source_digest, "EvaluatorIdentity.source_digest")
        if self.source_digest != _evaluator_source_digest(self.source_files):
            raise ValueError("EvaluatorIdentity.source_digest must be recomputable from source_files")
        if self.source_commit is not None and _GIT_REVISION_RE.fullmatch(self.source_commit) is None:
            raise ValueError("EvaluatorIdentity.source_commit must be a Git commit revision or None")
        if not isinstance(self.source_clean, bool):
            raise ValueError("EvaluatorIdentity.source_clean must be a boolean")
        if self.source_commit is None and self.source_clean:
            raise ValueError("EvaluatorIdentity.source_clean requires a recorded source_commit")
        _validate_absolute_path(self.production_root, "EvaluatorIdentity.production_root")
        _validate_digest_pairs(self.production_files, "EvaluatorIdentity.production_files")
        _validate_sha256(self.production_digest, "EvaluatorIdentity.production_digest")
        if self.production_digest != _production_source_digest(self.production_files):
            raise ValueError("EvaluatorIdentity.production_digest must be recomputable from production_files")
        if not isinstance(self.production_clean, bool):
            raise ValueError("EvaluatorIdentity.production_clean must be a boolean")
        if self.source_commit is None and self.production_clean:
            raise ValueError("EvaluatorIdentity.production_clean requires a recorded source_commit")
        _validate_absolute_path(self.host_python_path, "EvaluatorIdentity.host_python_path")
        _validate_absolute_path(self.host_python_realpath, "EvaluatorIdentity.host_python_realpath")
        _validate_sha256(self.host_python_sha256, "EvaluatorIdentity.host_python_sha256")
        _validate_non_empty_str(self.host_python_version, "EvaluatorIdentity.host_python_version")

    @staticmethod
    def build(
        *,
        source_files: tuple[tuple[str, str], ...],
        source_commit: str | None,
        source_clean: bool,
        production_root: str,
        production_files: tuple[tuple[str, str], ...],
        production_clean: bool,
        host_python_path: str,
        host_python_realpath: str,
        host_python_sha256: str,
        host_python_version: str,
    ) -> EvaluatorIdentity:
        return EvaluatorIdentity(
            source_digest=_evaluator_source_digest(source_files),
            source_files=source_files,
            source_commit=source_commit,
            source_clean=source_clean,
            production_root=production_root,
            production_digest=_production_source_digest(production_files),
            production_files=production_files,
            production_clean=production_clean,
            host_python_path=host_python_path,
            host_python_realpath=host_python_realpath,
            host_python_sha256=host_python_sha256,
            host_python_version=host_python_version,
        )

    def to_dict(self) -> dict[str, object]:
        return _evaluator_identity_to_dict(self)


def _evaluator_source_digest(source_files: tuple[tuple[str, str], ...]) -> str:
    return sha256_bytes(canonical_json({"source_files": [list(entry) for entry in source_files]}))


def _production_source_digest(production_files: tuple[tuple[str, str], ...]) -> str:
    return sha256_bytes(canonical_json({"production_files": [list(entry) for entry in production_files]}))


def _validate_digest_pairs(files: object, label: str) -> None:
    """One non-empty, canonically sorted closure of ``(relative path, SHA-256)`` pairs."""

    entries = _validate_tuple(files, label)
    if not entries:
        raise ValueError(f"{label} must not be empty")
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError(f"{label} entries must be (relative path, digest) tuples")
        name, digest = entry
        _validate_relative_path(name, f"{label} path")
        _validate_sha256(digest, f"{label}[{name}]")
        names.append(name)
    _validate_sorted_unique(names, label)


_EVALUATOR_IDENTITY_FIELDS = frozenset(
    {
        "source_digest",
        "source_files",
        "source_commit",
        "source_clean",
        "production_root",
        "production_digest",
        "production_files",
        "production_clean",
        "host_python_path",
        "host_python_realpath",
        "host_python_sha256",
        "host_python_version",
    }
)


def _evaluator_identity_to_dict(identity: EvaluatorIdentity) -> dict[str, object]:
    return {
        "source_digest": identity.source_digest,
        "source_files": [list(entry) for entry in identity.source_files],
        "source_commit": identity.source_commit,
        "source_clean": identity.source_clean,
        "production_root": identity.production_root,
        "production_digest": identity.production_digest,
        "production_files": [list(entry) for entry in identity.production_files],
        "production_clean": identity.production_clean,
        "host_python_path": identity.host_python_path,
        "host_python_realpath": identity.host_python_realpath,
        "host_python_sha256": identity.host_python_sha256,
        "host_python_version": identity.host_python_version,
    }


def _evaluator_identity_from_dict(value: object) -> EvaluatorIdentity:
    mapping = _expect_mapping(value, "EvaluatorIdentity")
    _closed_fields(mapping, _EVALUATOR_IDENTITY_FIELDS, "EvaluatorIdentity")
    return EvaluatorIdentity(
        source_digest=_expect_str(mapping["source_digest"], "EvaluatorIdentity.source_digest"),
        source_files=_expect_pair_list(mapping["source_files"], "EvaluatorIdentity.source_files"),
        source_commit=_optional_str(mapping["source_commit"], "EvaluatorIdentity.source_commit"),
        source_clean=_expect_bool(mapping["source_clean"], "EvaluatorIdentity.source_clean"),
        production_root=_expect_str(mapping["production_root"], "EvaluatorIdentity.production_root"),
        production_digest=_expect_str(mapping["production_digest"], "EvaluatorIdentity.production_digest"),
        production_files=_expect_pair_list(mapping["production_files"], "EvaluatorIdentity.production_files"),
        production_clean=_expect_bool(mapping["production_clean"], "EvaluatorIdentity.production_clean"),
        host_python_path=_expect_str(mapping["host_python_path"], "EvaluatorIdentity.host_python_path"),
        host_python_realpath=_expect_str(mapping["host_python_realpath"], "EvaluatorIdentity.host_python_realpath"),
        host_python_sha256=_expect_str(mapping["host_python_sha256"], "EvaluatorIdentity.host_python_sha256"),
        host_python_version=_expect_str(mapping["host_python_version"], "EvaluatorIdentity.host_python_version"),
    )


# --- BootstrapEnvironmentIdentity ------------------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrapEnvironmentIdentity:
    """The exact environment the resolver and installer received.

    Only key *names* and SHA-256 digests of values are recorded: a proxy URL with an
    embedded credential is never published in plaintext.  ``refused_keys`` names the
    ambient package-index, source, PATH, and ``PYTHONPATH`` controls that were present and
    deliberately not inherited.
    """

    inherited_keys: tuple[str, ...]
    inherited_value_digests: tuple[tuple[str, str], ...]
    service_keys: tuple[str, ...]
    refused_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        keys = _validate_string_tuple(self.inherited_keys, "BootstrapEnvironmentIdentity.inherited_keys")
        digests = _validate_tuple(
            self.inherited_value_digests, "BootstrapEnvironmentIdentity.inherited_value_digests"
        )
        digest_keys: list[str] = []
        for entry in digests:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("BootstrapEnvironmentIdentity.inherited_value_digests entries must be pairs")
            key, digest = entry
            _validate_non_empty_str(key, "BootstrapEnvironmentIdentity.inherited_value_digests key")
            _validate_sha256(digest, f"BootstrapEnvironmentIdentity.inherited_value_digests[{key}]")
            digest_keys.append(key)
        _validate_sorted_unique(digest_keys, "BootstrapEnvironmentIdentity.inherited_value_digests")
        if tuple(digest_keys) != keys:
            raise ValueError(
                "BootstrapEnvironmentIdentity.inherited_keys must match inherited_value_digests exactly"
            )
        service = _validate_string_tuple(self.service_keys, "BootstrapEnvironmentIdentity.service_keys")
        if not service:
            raise ValueError("BootstrapEnvironmentIdentity.service_keys must not be empty")
        refused = _validate_string_tuple(self.refused_keys, "BootstrapEnvironmentIdentity.refused_keys")
        for name in (*keys, *service, *refused):
            if _ENVIRONMENT_KEY_RE.fullmatch(name) is None:
                raise ValueError(f"BootstrapEnvironmentIdentity key is not an environment name: {name!r}")
        overlap = (set(keys) | set(service)) & set(refused)
        if overlap:
            raise ValueError(
                f"BootstrapEnvironmentIdentity.refused_keys cannot also be inherited: {sorted(overlap)}"
            )

    def to_dict(self) -> dict[str, object]:
        return _bootstrap_environment_to_dict(self)


_BOOTSTRAP_ENVIRONMENT_FIELDS = frozenset(
    {"inherited_keys", "inherited_value_digests", "service_keys", "refused_keys"}
)


def _bootstrap_environment_to_dict(identity: BootstrapEnvironmentIdentity) -> dict[str, object]:
    return {
        "inherited_keys": list(identity.inherited_keys),
        "inherited_value_digests": [list(entry) for entry in identity.inherited_value_digests],
        "service_keys": list(identity.service_keys),
        "refused_keys": list(identity.refused_keys),
    }


def _bootstrap_environment_from_dict(value: object) -> BootstrapEnvironmentIdentity:
    mapping = _expect_mapping(value, "BootstrapEnvironmentIdentity")
    _closed_fields(mapping, _BOOTSTRAP_ENVIRONMENT_FIELDS, "BootstrapEnvironmentIdentity")
    return BootstrapEnvironmentIdentity(
        inherited_keys=_expect_str_list(mapping["inherited_keys"], "BootstrapEnvironmentIdentity.inherited_keys"),
        inherited_value_digests=_expect_pair_list(
            mapping["inherited_value_digests"], "BootstrapEnvironmentIdentity.inherited_value_digests"
        ),
        service_keys=_expect_str_list(mapping["service_keys"], "BootstrapEnvironmentIdentity.service_keys"),
        refused_keys=_expect_str_list(mapping["refused_keys"], "BootstrapEnvironmentIdentity.refused_keys"),
    )


# --- RuntimeBinding ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    """The exact candidate runtime manifest one receipt is bound to."""

    root: str
    lock_digest: str
    manifest_path: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _validate_absolute_path(self.root, "RuntimeBinding.root")
        _validate_sha256(self.lock_digest, "RuntimeBinding.lock_digest")
        if self.root.rsplit("/", 1)[-1] != self.lock_digest:
            raise ValueError("RuntimeBinding.root must be addressed by the candidate lock digest")
        _validate_absolute_path(self.manifest_path, "RuntimeBinding.manifest_path")
        if not self.manifest_path.startswith(f"{self.root}/"):
            raise ValueError("RuntimeBinding.manifest_path must live inside the runtime root")
        _validate_sha256(self.manifest_sha256, "RuntimeBinding.manifest_sha256")

    def to_dict(self) -> dict[str, object]:
        return _runtime_binding_to_dict(self)


_RUNTIME_BINDING_FIELDS = frozenset({"root", "lock_digest", "manifest_path", "manifest_sha256"})


def _runtime_binding_to_dict(binding: RuntimeBinding) -> dict[str, object]:
    return {
        "root": binding.root,
        "lock_digest": binding.lock_digest,
        "manifest_path": binding.manifest_path,
        "manifest_sha256": binding.manifest_sha256,
    }


def _runtime_binding_from_dict(value: object) -> RuntimeBinding:
    mapping = _expect_mapping(value, "RuntimeBinding")
    _closed_fields(mapping, _RUNTIME_BINDING_FIELDS, "RuntimeBinding")
    return RuntimeBinding(
        root=_expect_str(mapping["root"], "RuntimeBinding.root"),
        lock_digest=_expect_str(mapping["lock_digest"], "RuntimeBinding.lock_digest"),
        manifest_path=_expect_str(mapping["manifest_path"], "RuntimeBinding.manifest_path"),
        manifest_sha256=_expect_str(mapping["manifest_sha256"], "RuntimeBinding.manifest_sha256"),
    )


# --- EnvironmentIdentity / ServiceConfigIdentity -------------------------------


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    name: str
    interpreter_path: str
    interpreter_realpath: str
    version: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "EnvironmentIdentity.name")
        _validate_absolute_path(self.interpreter_path, "EnvironmentIdentity.interpreter_path")
        _validate_absolute_path(self.interpreter_realpath, "EnvironmentIdentity.interpreter_realpath")
        _validate_non_empty_str(self.version, "EnvironmentIdentity.version")


_ENVIRONMENT_IDENTITY_FIELDS = frozenset({"name", "interpreter_path", "interpreter_realpath", "version"})


def _environment_identity_to_dict(identity: EnvironmentIdentity) -> dict[str, object]:
    return {
        "name": identity.name,
        "interpreter_path": identity.interpreter_path,
        "interpreter_realpath": identity.interpreter_realpath,
        "version": identity.version,
    }


def _environment_identity_from_dict(value: object) -> EnvironmentIdentity:
    mapping = _expect_mapping(value, "EnvironmentIdentity")
    _closed_fields(mapping, _ENVIRONMENT_IDENTITY_FIELDS, "EnvironmentIdentity")
    return EnvironmentIdentity(
        name=_expect_str(mapping["name"], "EnvironmentIdentity.name"),
        interpreter_path=_expect_str(mapping["interpreter_path"], "EnvironmentIdentity.interpreter_path"),
        interpreter_realpath=_expect_str(
            mapping["interpreter_realpath"], "EnvironmentIdentity.interpreter_realpath"
        ),
        version=_expect_str(mapping["version"], "EnvironmentIdentity.version"),
    )


@dataclass(frozen=True, slots=True)
class ServiceConfigIdentity:
    backend: str
    config_path: str
    config_sha256: str
    home_path: str
    cache_path: str

    def __post_init__(self) -> None:
        if self.backend not in _SERVICE_CONFIG_BACKENDS:
            raise ValueError(f"ServiceConfigIdentity.backend must be one of {sorted(_SERVICE_CONFIG_BACKENDS)}")
        _validate_absolute_path(self.config_path, "ServiceConfigIdentity.config_path")
        _validate_sha256(self.config_sha256, "ServiceConfigIdentity.config_sha256")
        _validate_absolute_path(self.home_path, "ServiceConfigIdentity.home_path")
        _validate_absolute_path(self.cache_path, "ServiceConfigIdentity.cache_path")


_SERVICE_CONFIG_IDENTITY_FIELDS = frozenset({"backend", "config_path", "config_sha256", "home_path", "cache_path"})


def _service_config_identity_to_dict(identity: ServiceConfigIdentity) -> dict[str, object]:
    return {
        "backend": identity.backend,
        "config_path": identity.config_path,
        "config_sha256": identity.config_sha256,
        "home_path": identity.home_path,
        "cache_path": identity.cache_path,
    }


def _service_config_identity_from_dict(value: object) -> ServiceConfigIdentity:
    mapping = _expect_mapping(value, "ServiceConfigIdentity")
    _closed_fields(mapping, _SERVICE_CONFIG_IDENTITY_FIELDS, "ServiceConfigIdentity")
    return ServiceConfigIdentity(
        backend=_expect_str(mapping["backend"], "ServiceConfigIdentity.backend"),
        config_path=_expect_str(mapping["config_path"], "ServiceConfigIdentity.config_path"),
        config_sha256=_expect_str(mapping["config_sha256"], "ServiceConfigIdentity.config_sha256"),
        home_path=_expect_str(mapping["home_path"], "ServiceConfigIdentity.home_path"),
        cache_path=_expect_str(mapping["cache_path"], "ServiceConfigIdentity.cache_path"),
    )


# --- ResolvedPackage / CandidatePackage / LockEvidence / CandidateLock ----------


@dataclass(frozen=True, slots=True)
class ResolvedPackage:
    name: str
    version: str
    requirement: str
    artifact_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "ResolvedPackage.name")
        _validate_non_empty_str(self.version, "ResolvedPackage.version")
        _validate_non_empty_str(self.requirement, "ResolvedPackage.requirement")
        _validate_artifact_hashes(self.artifact_hashes, f"ResolvedPackage.artifact_hashes[{self.name}]")


_RESOLVED_PACKAGE_FIELDS = frozenset({"name", "version", "requirement", "artifact_hashes"})


def _resolved_package_to_dict(package: ResolvedPackage) -> dict[str, object]:
    return {
        "name": package.name,
        "version": package.version,
        "requirement": package.requirement,
        "artifact_hashes": list(package.artifact_hashes),
    }


def _resolved_package_from_dict(value: object) -> ResolvedPackage:
    mapping = _expect_mapping(value, "ResolvedPackage")
    _closed_fields(mapping, _RESOLVED_PACKAGE_FIELDS, "ResolvedPackage")
    artifact_hashes = tuple(
        _expect_str(digest, "ResolvedPackage.artifact_hashes digest")
        for digest in _expect_list(mapping["artifact_hashes"], "ResolvedPackage.artifact_hashes")
    )
    return ResolvedPackage(
        name=_expect_str(mapping["name"], "ResolvedPackage.name"),
        version=_expect_str(mapping["version"], "ResolvedPackage.version"),
        requirement=_expect_str(mapping["requirement"], "ResolvedPackage.requirement"),
        artifact_hashes=artifact_hashes,
    )


@dataclass(frozen=True, slots=True)
class CandidatePackage:
    name: str
    version: str
    requirement: str
    artifact_hashes: tuple[str, ...]
    executable_relpath: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "CandidatePackage.name")
        _validate_non_empty_str(self.version, "CandidatePackage.version")
        _validate_non_empty_str(self.requirement, "CandidatePackage.requirement")
        _validate_relative_path(self.executable_relpath, "CandidatePackage.executable_relpath")
        _validate_artifact_hashes(self.artifact_hashes, f"CandidatePackage.artifact_hashes[{self.name}]")


_CANDIDATE_PACKAGE_FIELDS = frozenset({"name", "version", "requirement", "artifact_hashes", "executable_relpath"})


def _candidate_package_to_dict(package: CandidatePackage) -> dict[str, object]:
    return {
        "name": package.name,
        "version": package.version,
        "requirement": package.requirement,
        "artifact_hashes": list(package.artifact_hashes),
        "executable_relpath": package.executable_relpath,
    }


def _candidate_package_from_dict(value: object) -> CandidatePackage:
    mapping = _expect_mapping(value, "CandidatePackage")
    _closed_fields(mapping, _CANDIDATE_PACKAGE_FIELDS, "CandidatePackage")
    artifact_hashes = tuple(
        _expect_str(digest, "CandidatePackage.artifact_hashes digest")
        for digest in _expect_list(mapping["artifact_hashes"], "CandidatePackage.artifact_hashes")
    )
    return CandidatePackage(
        name=_expect_str(mapping["name"], "CandidatePackage.name"),
        version=_expect_str(mapping["version"], "CandidatePackage.version"),
        requirement=_expect_str(mapping["requirement"], "CandidatePackage.requirement"),
        artifact_hashes=artifact_hashes,
        executable_relpath=_expect_str(mapping["executable_relpath"], "CandidatePackage.executable_relpath"),
    )


@dataclass(frozen=True, slots=True)
class LockEvidence:
    """A bounded witness of the raw lock file the structured resolution was parsed from.

    ``CandidateLock.digest`` is the digest of raw bytes that the structured fields cannot
    reproduce.  This record states that explicitly: the raw digest and byte length are
    carried as a witness, and ``requirement_lines`` binds the raw parse to the structured
    resolution so a forged structure fails.
    """

    raw_sha256: str
    raw_size: int
    requirement_lines: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sha256(self.raw_sha256, "LockEvidence.raw_sha256")
        _validate_positive_int(self.raw_size, "LockEvidence.raw_size")
        lines = _validate_tuple(self.requirement_lines, "LockEvidence.requirement_lines")
        if not lines:
            raise ValueError("LockEvidence.requirement_lines must not be empty")
        for line in lines:
            _validate_non_empty_str(line, "LockEvidence.requirement_lines item")
        _validate_sorted_unique(cast("Sequence[str]", lines), "LockEvidence.requirement_lines")

    @staticmethod
    def derive_lines(resolved_packages: Sequence[ResolvedPackage]) -> tuple[str, ...]:
        return tuple(
            sorted(
                f"{package.requirement} " + " ".join(f"sha256:{digest}" for digest in package.artifact_hashes)
                for package in resolved_packages
            )
        )

    @staticmethod
    def build(*, raw_sha256: str, raw_size: int, resolved_packages: Sequence[ResolvedPackage]) -> LockEvidence:
        return LockEvidence(
            raw_sha256=raw_sha256,
            raw_size=raw_size,
            requirement_lines=LockEvidence.derive_lines(resolved_packages),
        )


_LOCK_EVIDENCE_FIELDS = frozenset({"raw_sha256", "raw_size", "requirement_lines"})


def _lock_evidence_to_dict(evidence: LockEvidence) -> dict[str, object]:
    return {
        "raw_sha256": evidence.raw_sha256,
        "raw_size": evidence.raw_size,
        "requirement_lines": list(evidence.requirement_lines),
    }


def _lock_evidence_from_dict(value: object) -> LockEvidence:
    mapping = _expect_mapping(value, "LockEvidence")
    _closed_fields(mapping, _LOCK_EVIDENCE_FIELDS, "LockEvidence")
    return LockEvidence(
        raw_sha256=_expect_str(mapping["raw_sha256"], "LockEvidence.raw_sha256"),
        raw_size=_expect_int(mapping["raw_size"], "LockEvidence.raw_size"),
        requirement_lines=_expect_str_list(mapping["requirement_lines"], "LockEvidence.requirement_lines"),
    )


@dataclass(frozen=True, slots=True)
class CandidateLock:
    digest: str
    exclude_newer: str
    resolved_packages: tuple[ResolvedPackage, ...]
    candidates: tuple[CandidatePackage, ...]
    lock_evidence: LockEvidence

    def __post_init__(self) -> None:
        _validate_sha256(self.digest, "CandidateLock.digest")
        _validate_utc_timestamp(self.exclude_newer, "CandidateLock.exclude_newer")
        resolved_packages = _validate_tuple(self.resolved_packages, "CandidateLock.resolved_packages")
        if not resolved_packages:
            raise ValueError("CandidateLock.resolved_packages must not be empty")
        _validate_sorted_unique([package.name for package in resolved_packages], "CandidateLock.resolved_packages")
        candidates = _validate_tuple(self.candidates, "CandidateLock.candidates")
        if len(candidates) != 2:
            raise ValueError("CandidateLock.candidates must contain exactly two entries")
        candidate_names = [package.name for package in candidates]
        _validate_sorted_unique(candidate_names, "CandidateLock.candidates")
        if set(candidate_names) != _CANDIDATE_NAMES:
            raise ValueError("CandidateLock.candidates must be exactly the ty and pyrefly candidates")
        resolved_by_name = {package.name: package for package in resolved_packages}
        for candidate in candidates:
            resolved = resolved_by_name.get(candidate.name)
            if resolved is None:
                raise ValueError(f"CandidateLock.candidates[{candidate.name}] is missing from resolved_packages")
            if (resolved.version, resolved.requirement, resolved.artifact_hashes) != (
                candidate.version,
                candidate.requirement,
                candidate.artifact_hashes,
            ):
                raise ValueError(
                    f"CandidateLock.candidates[{candidate.name}] does not match its resolved_packages entry"
                )
            if candidate.requirement != f"{candidate.name}=={candidate.version}":
                raise ValueError(f"CandidateLock.candidates[{candidate.name}] requirement is not an exact pin")
        if not isinstance(self.lock_evidence, LockEvidence):
            raise ValueError("CandidateLock.lock_evidence must be a LockEvidence record")
        if self.lock_evidence.raw_sha256 != self.digest:
            raise ValueError("CandidateLock.lock_evidence must witness the raw bytes CandidateLock.digest names")
        if self.lock_evidence.requirement_lines != LockEvidence.derive_lines(resolved_packages):
            raise ValueError("CandidateLock.lock_evidence requirement_lines contradict the resolved packages")


_CANDIDATE_LOCK_FIELDS = frozenset({"digest", "exclude_newer", "resolved_packages", "candidates", "lock_evidence"})


def _candidate_lock_to_dict(lock: CandidateLock) -> dict[str, object]:
    return {
        "digest": lock.digest,
        "exclude_newer": lock.exclude_newer,
        "resolved_packages": [_resolved_package_to_dict(package) for package in lock.resolved_packages],
        "candidates": [_candidate_package_to_dict(package) for package in lock.candidates],
        "lock_evidence": _lock_evidence_to_dict(lock.lock_evidence),
    }


def _candidate_lock_from_dict(value: object) -> CandidateLock:
    mapping = _expect_mapping(value, "CandidateLock")
    _closed_fields(mapping, _CANDIDATE_LOCK_FIELDS, "CandidateLock")
    resolved_packages = tuple(
        _resolved_package_from_dict(item)
        for item in _expect_list(mapping["resolved_packages"], "CandidateLock.resolved_packages")
    )
    candidates = tuple(
        _candidate_package_from_dict(item) for item in _expect_list(mapping["candidates"], "CandidateLock.candidates")
    )
    return CandidateLock(
        digest=_expect_str(mapping["digest"], "CandidateLock.digest"),
        exclude_newer=_expect_str(mapping["exclude_newer"], "CandidateLock.exclude_newer"),
        resolved_packages=resolved_packages,
        candidates=candidates,
        lock_evidence=_lock_evidence_from_dict(mapping["lock_evidence"]),
    )


# --- PathRecord / RootManifest -------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathRecord:
    path: str
    kind: str
    disposition: str
    size: int
    mtime_ns: int
    inode: int
    symlink_target: str | None
    content_sha256: str | None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, "PathRecord.path")
        if self.kind not in _PATH_RECORD_KINDS:
            raise ValueError(f"PathRecord.kind must be one of {sorted(_PATH_RECORD_KINDS)}")
        if self.disposition not in _PATH_RECORD_DISPOSITIONS:
            raise ValueError(f"PathRecord.disposition must be one of {sorted(_PATH_RECORD_DISPOSITIONS)}")
        _validate_int(self.size, "PathRecord.size", minimum=0)
        _validate_int(self.mtime_ns, "PathRecord.mtime_ns")
        _validate_int(self.inode, "PathRecord.inode", minimum=0)
        if self.kind == "symlink":
            _validate_non_empty_str(self.symlink_target, "PathRecord.symlink_target")
        elif self.symlink_target is not None:
            raise ValueError("PathRecord.symlink_target must be None unless kind is symlink")
        if self.content_sha256 is not None:
            if self.kind != "file":
                raise ValueError("PathRecord.content_sha256 must be None unless kind is file")
            _validate_sha256(self.content_sha256, "PathRecord.content_sha256")


_PATH_RECORD_FIELDS = frozenset(
    {"path", "kind", "disposition", "size", "mtime_ns", "inode", "symlink_target", "content_sha256"}
)


def _path_record_to_dict(record: PathRecord) -> dict[str, object]:
    return {
        "path": record.path,
        "kind": record.kind,
        "disposition": record.disposition,
        "size": record.size,
        "mtime_ns": record.mtime_ns,
        "inode": record.inode,
        "symlink_target": record.symlink_target,
        "content_sha256": record.content_sha256,
    }


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, label)


def _path_record_from_dict(value: object) -> PathRecord:
    mapping = _expect_mapping(value, "PathRecord")
    _closed_fields(mapping, _PATH_RECORD_FIELDS, "PathRecord")
    return PathRecord(
        path=_expect_str(mapping["path"], "PathRecord.path"),
        kind=_expect_str(mapping["kind"], "PathRecord.kind"),
        disposition=_expect_str(mapping["disposition"], "PathRecord.disposition"),
        size=_expect_int(mapping["size"], "PathRecord.size"),
        mtime_ns=_expect_int(mapping["mtime_ns"], "PathRecord.mtime_ns"),
        inode=_expect_int(mapping["inode"], "PathRecord.inode"),
        symlink_target=_optional_str(mapping["symlink_target"], "PathRecord.symlink_target"),
        content_sha256=_optional_str(mapping["content_sha256"], "PathRecord.content_sha256"),
    )


def root_manifest_digest(
    *,
    root: str,
    kind: str,
    source_revision: str | None,
    inventory_digest: str,
    inventory_paths: tuple[str, ...],
    excluded_paths: tuple[str, ...],
    hashed_paths: tuple[PathRecord, ...],
    metadata_paths: tuple[PathRecord, ...],
) -> str:
    """The canonical digest of one root manifest, recomputed from its own fields."""

    return sha256_bytes(
        canonical_json(
            {
                "root": root,
                "kind": kind,
                "source_revision": source_revision,
                "inventory_digest": inventory_digest,
                "inventory_paths": list(inventory_paths),
                "excluded_paths": list(excluded_paths),
                "hashed_paths": [_path_record_to_dict(record) for record in hashed_paths],
                "metadata_paths": [_path_record_to_dict(record) for record in metadata_paths],
            }
        )
    )


@dataclass(frozen=True, slots=True)
class RootManifest:
    root: str
    kind: str
    source_revision: str | None
    inventory_digest: str
    inventory_count: int
    inventory_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    hashed_paths: tuple[PathRecord, ...]
    metadata_paths: tuple[PathRecord, ...]
    manifest_digest: str

    def __post_init__(self) -> None:
        _validate_absolute_path(self.root, "RootManifest.root")
        if self.kind not in _ROOT_MANIFEST_KINDS:
            raise ValueError(f"RootManifest.kind must be one of {sorted(_ROOT_MANIFEST_KINDS)}")
        if self.kind == "git":
            if not isinstance(self.source_revision, str) or _GIT_REVISION_RE.fullmatch(self.source_revision) is None:
                raise ValueError("RootManifest.source_revision must be a Git commit revision when kind is git")
        elif self.source_revision is not None:
            raise ValueError("RootManifest.source_revision must be None when kind is non_git")
        _validate_sha256(self.inventory_digest, "RootManifest.inventory_digest")
        _validate_sha256(self.manifest_digest, "RootManifest.manifest_digest")
        _validate_int(self.inventory_count, "RootManifest.inventory_count", minimum=0)
        inventory_paths = _validate_tuple(self.inventory_paths, "RootManifest.inventory_paths")
        for path in inventory_paths:
            _validate_relative_path(path, "RootManifest.inventory_paths item")
        _validate_sorted_unique(cast("Sequence[str]", inventory_paths), "RootManifest.inventory_paths")
        excluded_paths = _validate_tuple(self.excluded_paths, "RootManifest.excluded_paths")
        for path in excluded_paths:
            _validate_relative_path(path, "RootManifest.excluded_paths item")
        _validate_sorted_unique(cast("Sequence[str]", excluded_paths), "RootManifest.excluded_paths")
        hashed_paths = _validate_tuple(self.hashed_paths, "RootManifest.hashed_paths")
        metadata_paths = _validate_tuple(self.metadata_paths, "RootManifest.metadata_paths")
        for record in hashed_paths:
            if record.content_sha256 is None:
                raise ValueError(f"RootManifest.hashed_paths[{record.path}] must have content_sha256")
        _validate_sorted_unique([record.path for record in hashed_paths], "RootManifest.hashed_paths")
        _validate_sorted_unique([record.path for record in metadata_paths], "RootManifest.metadata_paths")
        all_paths = [record.path for record in (*hashed_paths, *metadata_paths)]
        _validate_unique_names(all_paths, "RootManifest paths")
        if self.inventory_count != len(inventory_paths):
            raise ValueError("RootManifest.inventory_count must equal the number of carried inventory_paths")
        hashed_names = {record.path for record in hashed_paths}
        missing = sorted(set(inventory_paths) - hashed_names)
        if missing:
            raise ValueError(f"RootManifest.inventory_paths must be fully hashed: {missing[:5]}")
        expected = root_manifest_digest(
            root=self.root,
            kind=self.kind,
            source_revision=self.source_revision,
            inventory_digest=self.inventory_digest,
            inventory_paths=self.inventory_paths,
            excluded_paths=self.excluded_paths,
            hashed_paths=self.hashed_paths,
            metadata_paths=self.metadata_paths,
        )
        if self.manifest_digest != expected:
            raise ValueError("RootManifest.manifest_digest must be recomputable from its canonical fields")

    @staticmethod
    def build(
        *,
        root: str,
        kind: str,
        source_revision: str | None,
        inventory_digest: str,
        inventory_paths: tuple[str, ...],
        excluded_paths: tuple[str, ...],
        hashed_paths: tuple[PathRecord, ...],
        metadata_paths: tuple[PathRecord, ...],
    ) -> RootManifest:
        return RootManifest(
            root=root,
            kind=kind,
            source_revision=source_revision,
            inventory_digest=inventory_digest,
            inventory_count=len(inventory_paths),
            inventory_paths=inventory_paths,
            excluded_paths=excluded_paths,
            hashed_paths=hashed_paths,
            metadata_paths=metadata_paths,
            manifest_digest=root_manifest_digest(
                root=root,
                kind=kind,
                source_revision=source_revision,
                inventory_digest=inventory_digest,
                inventory_paths=inventory_paths,
                excluded_paths=excluded_paths,
                hashed_paths=hashed_paths,
                metadata_paths=metadata_paths,
            ),
        )

    @property
    def in_scope_count(self) -> int:
        return len(self.hashed_paths) + len(self.metadata_paths)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_paths)

    @property
    def observed_count(self) -> int:
        return self.in_scope_count + self.excluded_count

    def to_dict(self) -> dict[str, object]:
        return _root_manifest_to_dict(self)

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> RootManifest:
        return _root_manifest_from_dict(value)


_ROOT_MANIFEST_FIELDS = frozenset(
    {
        "root",
        "kind",
        "source_revision",
        "inventory_digest",
        "inventory_count",
        "inventory_paths",
        "excluded_paths",
        "hashed_paths",
        "metadata_paths",
        "manifest_digest",
    }
)


def _root_manifest_to_dict(manifest: RootManifest) -> dict[str, object]:
    return {
        "root": manifest.root,
        "kind": manifest.kind,
        "source_revision": manifest.source_revision,
        "inventory_digest": manifest.inventory_digest,
        "inventory_count": manifest.inventory_count,
        "inventory_paths": list(manifest.inventory_paths),
        "excluded_paths": list(manifest.excluded_paths),
        "hashed_paths": [_path_record_to_dict(record) for record in manifest.hashed_paths],
        "metadata_paths": [_path_record_to_dict(record) for record in manifest.metadata_paths],
        "manifest_digest": manifest.manifest_digest,
    }


def _root_manifest_from_dict(value: object) -> RootManifest:
    mapping = _expect_mapping(value, "RootManifest")
    _closed_fields(mapping, _ROOT_MANIFEST_FIELDS, "RootManifest")
    return RootManifest(
        root=_expect_str(mapping["root"], "RootManifest.root"),
        kind=_expect_str(mapping["kind"], "RootManifest.kind"),
        source_revision=_optional_str(mapping["source_revision"], "RootManifest.source_revision"),
        inventory_digest=_expect_str(mapping["inventory_digest"], "RootManifest.inventory_digest"),
        inventory_count=_expect_int(mapping["inventory_count"], "RootManifest.inventory_count"),
        inventory_paths=_expect_str_list(mapping["inventory_paths"], "RootManifest.inventory_paths"),
        excluded_paths=_expect_str_list(mapping["excluded_paths"], "RootManifest.excluded_paths"),
        hashed_paths=tuple(
            _path_record_from_dict(item) for item in _expect_list(mapping["hashed_paths"], "RootManifest.hashed_paths")
        ),
        metadata_paths=tuple(
            _path_record_from_dict(item)
            for item in _expect_list(mapping["metadata_paths"], "RootManifest.metadata_paths")
        ),
        manifest_digest=_expect_str(mapping["manifest_digest"], "RootManifest.manifest_digest"),
    )


# --- WriteDelta -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteDelta:
    root: str
    kind: str
    before_manifest_digest: str
    after_manifest_digest: str
    declared: tuple[str, ...]
    unexpected: tuple[str, ...]
    control_changes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_absolute_path(self.root, "WriteDelta.root")
        if self.kind not in _ROOT_MANIFEST_KINDS:
            raise ValueError(f"WriteDelta.kind must be one of {sorted(_ROOT_MANIFEST_KINDS)}")
        _validate_sha256(self.before_manifest_digest, "WriteDelta.before_manifest_digest")
        _validate_sha256(self.after_manifest_digest, "WriteDelta.after_manifest_digest")
        _validate_sorted_unique(_validate_tuple(self.declared, "WriteDelta.declared"), "WriteDelta.declared")
        _validate_sorted_unique(_validate_tuple(self.unexpected, "WriteDelta.unexpected"), "WriteDelta.unexpected")
        _validate_sorted_unique(
            _validate_tuple(self.control_changes, "WriteDelta.control_changes"), "WriteDelta.control_changes"
        )
        overlap = set(self.declared) & set(self.unexpected)
        if overlap:
            raise ValueError(f"WriteDelta declared and unexpected paths overlap: {sorted(overlap)}")


_WRITE_DELTA_FIELDS = frozenset(
    {
        "root",
        "kind",
        "before_manifest_digest",
        "after_manifest_digest",
        "declared",
        "unexpected",
        "control_changes",
    }
)


def _write_delta_to_dict(delta: WriteDelta) -> dict[str, object]:
    return {
        "root": delta.root,
        "kind": delta.kind,
        "before_manifest_digest": delta.before_manifest_digest,
        "after_manifest_digest": delta.after_manifest_digest,
        "declared": list(delta.declared),
        "unexpected": list(delta.unexpected),
        "control_changes": list(delta.control_changes),
    }


def _write_delta_from_dict(value: object) -> WriteDelta:
    mapping = _expect_mapping(value, "WriteDelta")
    _closed_fields(mapping, _WRITE_DELTA_FIELDS, "WriteDelta")
    return WriteDelta(
        root=_expect_str(mapping["root"], "WriteDelta.root"),
        kind=_expect_str(mapping["kind"], "WriteDelta.kind"),
        before_manifest_digest=_expect_str(mapping["before_manifest_digest"], "WriteDelta.before_manifest_digest"),
        after_manifest_digest=_expect_str(mapping["after_manifest_digest"], "WriteDelta.after_manifest_digest"),
        declared=_expect_str_list(mapping["declared"], "WriteDelta.declared"),
        unexpected=_expect_str_list(mapping["unexpected"], "WriteDelta.unexpected"),
        control_changes=_expect_str_list(mapping["control_changes"], "WriteDelta.control_changes"),
    )


# --- AdmissionReceipt ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    schema_version: int
    evaluation_contract_version: str
    evaluation_identity: str
    run_identity: str
    status: str
    started_at: str
    ended_at: str
    budgets: tuple[PhaseBudget, ...]
    evaluator: EvaluatorIdentity | None
    bootstrap_environment: BootstrapEnvironmentIdentity | None
    runtime_binding: RuntimeBinding | None
    production_identity_before: ProductionIdentity
    production_identity_after: ProductionIdentity
    candidate_lock: CandidateLock
    environments: tuple[EnvironmentIdentity, ...]
    service_configs: tuple[ServiceConfigIdentity, ...]
    root_manifests_before: tuple[RootManifest, ...]
    root_manifests_after: tuple[RootManifest, ...]
    write_deltas: tuple[WriteDelta, ...]
    issues: tuple[str, ...]
    artifact_tree_digest: str
    next_action: str

    def __post_init__(self) -> None:
        if self.schema_version != ADMISSION_RECEIPT_SCHEMA_VERSION:
            raise ValueError(
                f"AdmissionReceipt schema_version must be {ADMISSION_RECEIPT_SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if self.evaluation_contract_version != EVALUATION_CONTRACT_VERSION:
            raise ValueError(
                f"AdmissionReceipt evaluation_contract_version must be {EVALUATION_CONTRACT_VERSION!r}, "
                f"got {self.evaluation_contract_version!r}"
            )
        _validate_sha256(self.evaluation_identity, "AdmissionReceipt.evaluation_identity")
        _validate_sha256(self.run_identity, "AdmissionReceipt.run_identity")
        if self.status not in _ADMISSION_STATUSES:
            raise ValueError(f"AdmissionReceipt.status must be one of {sorted(_ADMISSION_STATUSES)}")
        _validate_non_empty_str(self.started_at, "AdmissionReceipt.started_at")
        _validate_non_empty_str(self.ended_at, "AdmissionReceipt.ended_at")
        budgets = _validate_tuple(self.budgets, "AdmissionReceipt.budgets")
        _validate_sorted_unique([budget.name for budget in budgets], "AdmissionReceipt.budgets")
        environments = _validate_tuple(self.environments, "AdmissionReceipt.environments")
        _validate_sorted_unique([identity.name for identity in environments], "AdmissionReceipt.environments")
        service_configs = _validate_tuple(self.service_configs, "AdmissionReceipt.service_configs")
        _validate_sorted_unique(
            [identity.backend for identity in service_configs], "AdmissionReceipt.service_configs"
        )
        root_manifests_before = _validate_tuple(self.root_manifests_before, "AdmissionReceipt.root_manifests_before")
        _validate_sorted_unique(
            [manifest.root for manifest in root_manifests_before], "AdmissionReceipt.root_manifests_before"
        )
        root_manifests_after = _validate_tuple(self.root_manifests_after, "AdmissionReceipt.root_manifests_after")
        _validate_sorted_unique(
            [manifest.root for manifest in root_manifests_after], "AdmissionReceipt.root_manifests_after"
        )
        write_deltas = _validate_tuple(self.write_deltas, "AdmissionReceipt.write_deltas")
        _validate_sorted_unique([delta.root for delta in write_deltas], "AdmissionReceipt.write_deltas")
        issues = _validate_tuple(self.issues, "AdmissionReceipt.issues")
        _validate_sorted_unique(issues, "AdmissionReceipt.issues")
        _validate_sha256(self.artifact_tree_digest, "AdmissionReceipt.artifact_tree_digest")
        _validate_non_empty_str(self.next_action, "AdmissionReceipt.next_action")
        if self.status == "pass":
            self._require_canonical_pass(
                budgets, environments, service_configs, root_manifests_before, root_manifests_after, write_deltas
            )

    def _require_canonical_pass(
        self,
        budgets: tuple[PhaseBudget, ...],
        environments: tuple[EnvironmentIdentity, ...],
        service_configs: tuple[ServiceConfigIdentity, ...],
        root_manifests_before: tuple[RootManifest, ...],
        root_manifests_after: tuple[RootManifest, ...],
        write_deltas: tuple[WriteDelta, ...],
    ) -> None:
        """A ``pass`` receipt must carry the complete Phase 1 evidence set."""

        if self.issues:
            raise ValueError("AdmissionReceipt status is pass but it carries issues")
        if self.next_action != NEXT_ACTION_PASS:
            raise ValueError(f"AdmissionReceipt status is pass but next_action is not {NEXT_ACTION_PASS!r}")
        started = _validate_utc_timestamp(self.started_at, "AdmissionReceipt.started_at")
        ended = _validate_utc_timestamp(self.ended_at, "AdmissionReceipt.ended_at")
        if ended < started:
            raise ValueError("AdmissionReceipt.ended_at must not precede started_at")
        # Every frozen budget, by name *and* by exact seconds: a pass may not silently
        # widen the admission ceiling or any later phase's ceiling.
        if tuple(sorted(budgets, key=lambda budget: budget.name)) != default_phase_budgets():
            observed = sorted((budget.name, budget.seconds) for budget in budgets)
            raise ValueError(
                f"AdmissionReceipt status is pass but budgets are not the frozen Phase 1 set: {observed}"
            )
        if self.evaluator is None:
            raise ValueError("AdmissionReceipt status is pass but no evaluator identity is bound")
        if self.bootstrap_environment is None:
            raise ValueError("AdmissionReceipt status is pass but no bootstrap_environment identity is bound")
        if self.runtime_binding is None:
            raise ValueError("AdmissionReceipt status is pass but no runtime_binding is recorded")
        if self.runtime_binding.lock_digest != self.candidate_lock.digest:
            raise ValueError("AdmissionReceipt status is pass but runtime_binding names another candidate lock")
        if {identity.name for identity in environments} != _REQUIRED_ENVIRONMENT_NAMES:
            raise ValueError(
                f"AdmissionReceipt status is pass but environments are not {sorted(_REQUIRED_ENVIRONMENT_NAMES)}"
            )
        if {identity.backend for identity in service_configs} != _SERVICE_CONFIG_BACKENDS:
            raise ValueError(
                f"AdmissionReceipt status is pass but service_configs are not {sorted(_SERVICE_CONFIG_BACKENDS)}"
            )
        if self.production_identity_before != self.production_identity_after:
            raise ValueError(
                "AdmissionReceipt status is pass but production identity changed between before and after"
            )
        before_by_root = {manifest.root: manifest for manifest in root_manifests_before}
        after_by_root = {manifest.root: manifest for manifest in root_manifests_after}
        delta_roots = {delta.root for delta in write_deltas}
        if not before_by_root:
            raise ValueError("AdmissionReceipt status is pass but it carries no root manifest")
        if set(before_by_root) != delta_roots or set(after_by_root) != delta_roots:
            raise ValueError(
                "AdmissionReceipt status is pass but root manifest roots do not match write_deltas roots"
            )
        for delta in write_deltas:
            before_manifest = before_by_root[delta.root]
            after_manifest = after_by_root[delta.root]
            if delta.before_manifest_digest != before_manifest.manifest_digest:
                raise ValueError(
                    f"AdmissionReceipt status is pass but write_deltas[{delta.root}]."
                    "before_manifest_digest does not match its root_manifests_before entry"
                )
            if delta.after_manifest_digest != after_manifest.manifest_digest:
                raise ValueError(
                    f"AdmissionReceipt status is pass but write_deltas[{delta.root}]."
                    "after_manifest_digest does not match its root_manifests_after entry"
                )
            if delta.unexpected:
                raise ValueError(
                    f"AdmissionReceipt status is pass but write_deltas[{delta.root}] has unexpected paths"
                )
            if delta.declared:
                raise ValueError(
                    f"AdmissionReceipt status is pass but write_deltas[{delta.root}] has declared mutations; "
                    "admission declares none"
                )
            if delta.control_changes:
                raise ValueError(
                    f"AdmissionReceipt status is pass but write_deltas[{delta.root}] has control_changes"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluation_contract_version": self.evaluation_contract_version,
            "evaluation_identity": self.evaluation_identity,
            "run_identity": self.run_identity,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "budgets": [_phase_budget_to_dict(budget) for budget in self.budgets],
            "evaluator": None if self.evaluator is None else _evaluator_identity_to_dict(self.evaluator),
            "bootstrap_environment": (
                None
                if self.bootstrap_environment is None
                else _bootstrap_environment_to_dict(self.bootstrap_environment)
            ),
            "runtime_binding": (
                None if self.runtime_binding is None else _runtime_binding_to_dict(self.runtime_binding)
            ),
            "production_identity_before": _production_identity_to_dict(self.production_identity_before),
            "production_identity_after": _production_identity_to_dict(self.production_identity_after),
            "candidate_lock": _candidate_lock_to_dict(self.candidate_lock),
            "environments": [_environment_identity_to_dict(identity) for identity in self.environments],
            "service_configs": [_service_config_identity_to_dict(identity) for identity in self.service_configs],
            "root_manifests_before": [_root_manifest_to_dict(manifest) for manifest in self.root_manifests_before],
            "root_manifests_after": [_root_manifest_to_dict(manifest) for manifest in self.root_manifests_after],
            "write_deltas": [_write_delta_to_dict(delta) for delta in self.write_deltas],
            "issues": list(self.issues),
            "artifact_tree_digest": self.artifact_tree_digest,
            "next_action": self.next_action,
        }

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> AdmissionReceipt:
        schema_version = value.get("schema_version")
        if schema_version != ADMISSION_RECEIPT_SCHEMA_VERSION:
            raise ValueError(
                f"AdmissionReceipt schema_version must be {ADMISSION_RECEIPT_SCHEMA_VERSION}, got {schema_version!r}"
            )
        _closed_fields(value, _ADMISSION_RECEIPT_FIELDS, "AdmissionReceipt")
        evaluator = value["evaluator"]
        bootstrap = value["bootstrap_environment"]
        binding = value["runtime_binding"]
        return AdmissionReceipt(
            schema_version=_expect_int(value["schema_version"], "AdmissionReceipt.schema_version"),
            evaluation_contract_version=_expect_str(
                value["evaluation_contract_version"], "AdmissionReceipt.evaluation_contract_version"
            ),
            evaluation_identity=_expect_str(value["evaluation_identity"], "AdmissionReceipt.evaluation_identity"),
            run_identity=_expect_str(value["run_identity"], "AdmissionReceipt.run_identity"),
            status=_expect_str(value["status"], "AdmissionReceipt.status"),
            started_at=_expect_str(value["started_at"], "AdmissionReceipt.started_at"),
            ended_at=_expect_str(value["ended_at"], "AdmissionReceipt.ended_at"),
            budgets=tuple(
                _phase_budget_from_dict(item) for item in _expect_list(value["budgets"], "AdmissionReceipt.budgets")
            ),
            evaluator=None if evaluator is None else _evaluator_identity_from_dict(evaluator),
            bootstrap_environment=None if bootstrap is None else _bootstrap_environment_from_dict(bootstrap),
            runtime_binding=None if binding is None else _runtime_binding_from_dict(binding),
            production_identity_before=_production_identity_from_dict(value["production_identity_before"]),
            production_identity_after=_production_identity_from_dict(value["production_identity_after"]),
            candidate_lock=_candidate_lock_from_dict(value["candidate_lock"]),
            environments=tuple(
                _environment_identity_from_dict(item)
                for item in _expect_list(value["environments"], "AdmissionReceipt.environments")
            ),
            service_configs=tuple(
                _service_config_identity_from_dict(item)
                for item in _expect_list(value["service_configs"], "AdmissionReceipt.service_configs")
            ),
            root_manifests_before=tuple(
                _root_manifest_from_dict(item)
                for item in _expect_list(value["root_manifests_before"], "AdmissionReceipt.root_manifests_before")
            ),
            root_manifests_after=tuple(
                _root_manifest_from_dict(item)
                for item in _expect_list(value["root_manifests_after"], "AdmissionReceipt.root_manifests_after")
            ),
            write_deltas=tuple(
                _write_delta_from_dict(item)
                for item in _expect_list(value["write_deltas"], "AdmissionReceipt.write_deltas")
            ),
            issues=tuple(
                _expect_str(item, "AdmissionReceipt.issues item")
                for item in _expect_list(value["issues"], "AdmissionReceipt.issues")
            ),
            artifact_tree_digest=_expect_str(value["artifact_tree_digest"], "AdmissionReceipt.artifact_tree_digest"),
            next_action=_expect_str(value["next_action"], "AdmissionReceipt.next_action"),
        )


_ADMISSION_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_contract_version",
        "evaluation_identity",
        "run_identity",
        "status",
        "started_at",
        "ended_at",
        "budgets",
        "evaluator",
        "bootstrap_environment",
        "runtime_binding",
        "production_identity_before",
        "production_identity_after",
        "candidate_lock",
        "environments",
        "service_configs",
        "root_manifests_before",
        "root_manifests_after",
        "write_deltas",
        "issues",
        "artifact_tree_digest",
        "next_action",
    }
)


# --- Protocol-phase evidence (Phase 2 Task 1) ------------------------------------
#
# CapabilityEvidence, LifecycleEvidence, and CandidateProtocolOutcome are leaf records
# nested inside ProtocolPhaseReceipt, following PathRecord/WriteDelta's precedent: only
# private module-level (de)serializers, no public to_dict/from_dict. ProtocolPhaseReceipt
# itself mirrors AdmissionReceipt's public to_dict/from_dict/closed-field/canonical-order
# discipline exactly, because it is this phase's published, top-level receipt.

PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION = 1

# Decision P2-3: task_utility is a fixed disposition literal, never fabricated Phase 2 data.
CAPABILITY_TASK_UTILITY_DEFERRED = "deferred_to_feature_phase"

_CANDIDATE_PROTOCOL_NAMES = frozenset({"pyright", "ty", "pyrefly"})
_GATE_DISPOSITIONS = frozenset({"pass", "fail", "seam_incompatible_pull_only"})
_DIAGNOSTICS_MODES = frozenset({"push", "pull"})


def _validate_diagnostics_mode(value: object, label: str) -> str:
    text = _validate_non_empty_str(value, label)
    if text not in _DIAGNOSTICS_MODES:
        raise ValueError(f"{label} must be one of {sorted(_DIAGNOSTICS_MODES)}")
    return text


def _validate_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _validate_optional_bool(value: object, label: str) -> bool | None:
    if value is None:
        return None
    return _validate_bool(value, label)


def _validate_non_negative_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a number")
    if value < 0:
        raise ValueError(f"{label} must be >= 0")
    return float(value)


def _optional_bool(value: object, label: str) -> bool | None:
    if value is None:
        return None
    return _expect_bool(value, label)


def _expect_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a number")
    return float(value)


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """One capability's advertisement/accept/normalize/utility evidence.

    ``task_utility`` is fixed to :data:`CAPABILITY_TASK_UTILITY_DEFERRED` on every Phase 2
    record (Decision P2-3): real-task utility is Phase 4's decision-owning evidence, and
    Phase 2 never fabricates it.
    """

    name: str
    advertised: bool
    accepted: bool | None
    normalized_valid: bool | None
    task_utility: str
    notes: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "CapabilityEvidence.name")
        _validate_bool(self.advertised, "CapabilityEvidence.advertised")
        _validate_optional_bool(self.accepted, "CapabilityEvidence.accepted")
        _validate_optional_bool(self.normalized_valid, "CapabilityEvidence.normalized_valid")
        if self.task_utility != CAPABILITY_TASK_UTILITY_DEFERRED:
            raise ValueError(
                f"CapabilityEvidence.task_utility must be {CAPABILITY_TASK_UTILITY_DEFERRED!r}, "
                f"got {self.task_utility!r}"
            )
        if not isinstance(self.notes, str):
            raise ValueError("CapabilityEvidence.notes must be a string")


_CAPABILITY_EVIDENCE_FIELDS = frozenset(
    {"name", "advertised", "accepted", "normalized_valid", "task_utility", "notes"}
)


def _capability_evidence_to_dict(evidence: CapabilityEvidence) -> dict[str, object]:
    return {
        "name": evidence.name,
        "advertised": evidence.advertised,
        "accepted": evidence.accepted,
        "normalized_valid": evidence.normalized_valid,
        "task_utility": evidence.task_utility,
        "notes": evidence.notes,
    }


def _capability_evidence_from_dict(value: object) -> CapabilityEvidence:
    mapping = _expect_mapping(value, "CapabilityEvidence")
    _closed_fields(mapping, _CAPABILITY_EVIDENCE_FIELDS, "CapabilityEvidence")
    return CapabilityEvidence(
        name=_expect_str(mapping["name"], "CapabilityEvidence.name"),
        advertised=_expect_bool(mapping["advertised"], "CapabilityEvidence.advertised"),
        accepted=_optional_bool(mapping["accepted"], "CapabilityEvidence.accepted"),
        normalized_valid=_optional_bool(mapping["normalized_valid"], "CapabilityEvidence.normalized_valid"),
        task_utility=_expect_str(mapping["task_utility"], "CapabilityEvidence.task_utility"),
        notes=_expect_str(mapping["notes"], "CapabilityEvidence.notes"),
    )


@dataclass(frozen=True, slots=True)
class LifecycleEvidence:
    cold_readiness_seconds: float
    diagnostics_mode: str
    content_modified_count: int
    request_cancelled_count: int
    retry_seam_disabled: bool
    bounded_timeout_observed: bool
    crash_handled: bool
    shutdown_clean: bool
    cleanup_clean: bool
    proxy_rejected: bool
    minimal_environment_verified: bool
    redaction_verified: bool

    def __post_init__(self) -> None:
        _validate_non_negative_number(self.cold_readiness_seconds, "LifecycleEvidence.cold_readiness_seconds")
        _validate_diagnostics_mode(self.diagnostics_mode, "LifecycleEvidence.diagnostics_mode")
        _validate_int(self.content_modified_count, "LifecycleEvidence.content_modified_count", minimum=0)
        _validate_int(self.request_cancelled_count, "LifecycleEvidence.request_cancelled_count", minimum=0)
        for name in (
            "retry_seam_disabled",
            "bounded_timeout_observed",
            "crash_handled",
            "shutdown_clean",
            "cleanup_clean",
            "proxy_rejected",
            "minimal_environment_verified",
            "redaction_verified",
        ):
            _validate_bool(getattr(self, name), f"LifecycleEvidence.{name}")


_LIFECYCLE_EVIDENCE_FIELDS = frozenset(
    {
        "cold_readiness_seconds",
        "diagnostics_mode",
        "content_modified_count",
        "request_cancelled_count",
        "retry_seam_disabled",
        "bounded_timeout_observed",
        "crash_handled",
        "shutdown_clean",
        "cleanup_clean",
        "proxy_rejected",
        "minimal_environment_verified",
        "redaction_verified",
    }
)


def _lifecycle_evidence_to_dict(evidence: LifecycleEvidence) -> dict[str, object]:
    return {
        "cold_readiness_seconds": evidence.cold_readiness_seconds,
        "diagnostics_mode": evidence.diagnostics_mode,
        "content_modified_count": evidence.content_modified_count,
        "request_cancelled_count": evidence.request_cancelled_count,
        "retry_seam_disabled": evidence.retry_seam_disabled,
        "bounded_timeout_observed": evidence.bounded_timeout_observed,
        "crash_handled": evidence.crash_handled,
        "shutdown_clean": evidence.shutdown_clean,
        "cleanup_clean": evidence.cleanup_clean,
        "proxy_rejected": evidence.proxy_rejected,
        "minimal_environment_verified": evidence.minimal_environment_verified,
        "redaction_verified": evidence.redaction_verified,
    }


def _lifecycle_evidence_from_dict(value: object) -> LifecycleEvidence:
    mapping = _expect_mapping(value, "LifecycleEvidence")
    _closed_fields(mapping, _LIFECYCLE_EVIDENCE_FIELDS, "LifecycleEvidence")
    return LifecycleEvidence(
        cold_readiness_seconds=_expect_number(
            mapping["cold_readiness_seconds"], "LifecycleEvidence.cold_readiness_seconds"
        ),
        diagnostics_mode=_expect_str(mapping["diagnostics_mode"], "LifecycleEvidence.diagnostics_mode"),
        content_modified_count=_expect_int(
            mapping["content_modified_count"], "LifecycleEvidence.content_modified_count"
        ),
        request_cancelled_count=_expect_int(
            mapping["request_cancelled_count"], "LifecycleEvidence.request_cancelled_count"
        ),
        retry_seam_disabled=_expect_bool(mapping["retry_seam_disabled"], "LifecycleEvidence.retry_seam_disabled"),
        bounded_timeout_observed=_expect_bool(
            mapping["bounded_timeout_observed"], "LifecycleEvidence.bounded_timeout_observed"
        ),
        crash_handled=_expect_bool(mapping["crash_handled"], "LifecycleEvidence.crash_handled"),
        shutdown_clean=_expect_bool(mapping["shutdown_clean"], "LifecycleEvidence.shutdown_clean"),
        cleanup_clean=_expect_bool(mapping["cleanup_clean"], "LifecycleEvidence.cleanup_clean"),
        proxy_rejected=_expect_bool(mapping["proxy_rejected"], "LifecycleEvidence.proxy_rejected"),
        minimal_environment_verified=_expect_bool(
            mapping["minimal_environment_verified"], "LifecycleEvidence.minimal_environment_verified"
        ),
        redaction_verified=_expect_bool(mapping["redaction_verified"], "LifecycleEvidence.redaction_verified"),
    )


_RAW_LSP_PROVIDERS_FIELDS = frozenset(
    {"definition", "declaration", "implementation", "references", "document_symbols", "workspace_symbols"}
)


def _raw_lsp_providers_to_dict(providers: RawLspProviders) -> dict[str, object]:
    return {
        "definition": providers.definition,
        "declaration": providers.declaration,
        "implementation": providers.implementation,
        "references": providers.references,
        "document_symbols": providers.document_symbols,
        "workspace_symbols": providers.workspace_symbols,
    }


def _raw_lsp_providers_from_dict(value: object) -> RawLspProviders:
    from serena_light.lsp.adapter import RawLspProviders  # see module-level TYPE_CHECKING note

    mapping = _expect_mapping(value, "RawLspProviders")
    _closed_fields(mapping, _RAW_LSP_PROVIDERS_FIELDS, "RawLspProviders")
    return RawLspProviders(
        definition=_expect_bool(mapping["definition"], "RawLspProviders.definition"),
        declaration=_expect_bool(mapping["declaration"], "RawLspProviders.declaration"),
        implementation=_expect_bool(mapping["implementation"], "RawLspProviders.implementation"),
        references=_expect_bool(mapping["references"], "RawLspProviders.references"),
        document_symbols=_expect_bool(mapping["document_symbols"], "RawLspProviders.document_symbols"),
        workspace_symbols=_expect_bool(mapping["workspace_symbols"], "RawLspProviders.workspace_symbols"),
    )


@dataclass(frozen=True, slots=True)
class CandidateProtocolOutcome:
    candidate: str
    engine_version: str
    raw_providers: RawLspProviders
    capabilities: tuple[CapabilityEvidence, ...]
    lifecycle: LifecycleEvidence
    gate_disposition: str
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        from serena_light.lsp.adapter import RawLspProviders  # see module-level TYPE_CHECKING note

        if self.candidate not in _CANDIDATE_PROTOCOL_NAMES:
            raise ValueError(f"CandidateProtocolOutcome.candidate must be one of {sorted(_CANDIDATE_PROTOCOL_NAMES)}")
        _validate_non_empty_str(self.engine_version, "CandidateProtocolOutcome.engine_version")
        if not isinstance(self.raw_providers, RawLspProviders):
            raise ValueError("CandidateProtocolOutcome.raw_providers must be a RawLspProviders record")
        capabilities = _validate_tuple(self.capabilities, "CandidateProtocolOutcome.capabilities")
        _validate_sorted_unique(
            [capability.name for capability in capabilities], "CandidateProtocolOutcome.capabilities"
        )
        if not isinstance(self.lifecycle, LifecycleEvidence):
            raise ValueError("CandidateProtocolOutcome.lifecycle must be a LifecycleEvidence record")
        if self.gate_disposition not in _GATE_DISPOSITIONS:
            raise ValueError(f"CandidateProtocolOutcome.gate_disposition must be one of {sorted(_GATE_DISPOSITIONS)}")
        _validate_sorted_unique(
            _validate_tuple(self.issues, "CandidateProtocolOutcome.issues"), "CandidateProtocolOutcome.issues"
        )


_CANDIDATE_PROTOCOL_OUTCOME_FIELDS = frozenset(
    {"candidate", "engine_version", "raw_providers", "capabilities", "lifecycle", "gate_disposition", "issues"}
)


def _candidate_protocol_outcome_to_dict(outcome: CandidateProtocolOutcome) -> dict[str, object]:
    return {
        "candidate": outcome.candidate,
        "engine_version": outcome.engine_version,
        "raw_providers": _raw_lsp_providers_to_dict(outcome.raw_providers),
        "capabilities": [_capability_evidence_to_dict(capability) for capability in outcome.capabilities],
        "lifecycle": _lifecycle_evidence_to_dict(outcome.lifecycle),
        "gate_disposition": outcome.gate_disposition,
        "issues": list(outcome.issues),
    }


def _candidate_protocol_outcome_from_dict(value: object) -> CandidateProtocolOutcome:
    mapping = _expect_mapping(value, "CandidateProtocolOutcome")
    _closed_fields(mapping, _CANDIDATE_PROTOCOL_OUTCOME_FIELDS, "CandidateProtocolOutcome")
    return CandidateProtocolOutcome(
        candidate=_expect_str(mapping["candidate"], "CandidateProtocolOutcome.candidate"),
        engine_version=_expect_str(mapping["engine_version"], "CandidateProtocolOutcome.engine_version"),
        raw_providers=_raw_lsp_providers_from_dict(mapping["raw_providers"]),
        capabilities=tuple(
            _capability_evidence_from_dict(item)
            for item in _expect_list(mapping["capabilities"], "CandidateProtocolOutcome.capabilities")
        ),
        lifecycle=_lifecycle_evidence_from_dict(mapping["lifecycle"]),
        gate_disposition=_expect_str(mapping["gate_disposition"], "CandidateProtocolOutcome.gate_disposition"),
        issues=tuple(
            _expect_str(item, "CandidateProtocolOutcome.issues item")
            for item in _expect_list(mapping["issues"], "CandidateProtocolOutcome.issues")
        ),
    )


@dataclass(frozen=True, slots=True)
class ProtocolPhaseReceipt:
    """The Phase 2 protocol-plane receipt, mirroring :class:`AdmissionReceipt` exactly.

    Unlike :class:`AdmissionReceipt`, this receipt carries one :class:`CandidateProtocolOutcome`
    per candidate instead of the fixed environment/service-config evidence, and a ``pass``
    receipt requires only the frozen ``protocol`` budget rather than the complete Phase 1
    budget set, because this receipt binds one phase, not the whole evaluation.
    """

    schema_version: int
    evaluation_contract_version: str
    evaluation_identity: str
    run_identity: str
    status: str
    started_at: str
    ended_at: str
    budgets: tuple[PhaseBudget, ...]
    evaluator: EvaluatorIdentity | None
    production_identity_before: ProductionIdentity
    production_identity_after: ProductionIdentity
    candidate_lock: CandidateLock
    runtime_binding: RuntimeBinding | None
    root_manifests_before: tuple[RootManifest, ...]
    root_manifests_after: tuple[RootManifest, ...]
    write_deltas: tuple[WriteDelta, ...]
    outcomes: tuple[CandidateProtocolOutcome, ...]
    issues: tuple[str, ...]
    artifact_tree_digest: str
    next_action: str

    def __post_init__(self) -> None:
        if self.schema_version != PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION:
            raise ValueError(
                f"ProtocolPhaseReceipt schema_version must be {PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if self.evaluation_contract_version != EVALUATION_CONTRACT_VERSION:
            raise ValueError(
                f"ProtocolPhaseReceipt evaluation_contract_version must be {EVALUATION_CONTRACT_VERSION!r}, "
                f"got {self.evaluation_contract_version!r}"
            )
        _validate_sha256(self.evaluation_identity, "ProtocolPhaseReceipt.evaluation_identity")
        _validate_sha256(self.run_identity, "ProtocolPhaseReceipt.run_identity")
        if self.status not in _ADMISSION_STATUSES:
            raise ValueError(f"ProtocolPhaseReceipt.status must be one of {sorted(_ADMISSION_STATUSES)}")
        _validate_non_empty_str(self.started_at, "ProtocolPhaseReceipt.started_at")
        _validate_non_empty_str(self.ended_at, "ProtocolPhaseReceipt.ended_at")
        budgets = _validate_tuple(self.budgets, "ProtocolPhaseReceipt.budgets")
        _validate_sorted_unique([budget.name for budget in budgets], "ProtocolPhaseReceipt.budgets")
        root_manifests_before = _validate_tuple(
            self.root_manifests_before, "ProtocolPhaseReceipt.root_manifests_before"
        )
        _validate_sorted_unique(
            [manifest.root for manifest in root_manifests_before], "ProtocolPhaseReceipt.root_manifests_before"
        )
        root_manifests_after = _validate_tuple(self.root_manifests_after, "ProtocolPhaseReceipt.root_manifests_after")
        _validate_sorted_unique(
            [manifest.root for manifest in root_manifests_after], "ProtocolPhaseReceipt.root_manifests_after"
        )
        write_deltas = _validate_tuple(self.write_deltas, "ProtocolPhaseReceipt.write_deltas")
        _validate_sorted_unique([delta.root for delta in write_deltas], "ProtocolPhaseReceipt.write_deltas")
        outcomes = _validate_tuple(self.outcomes, "ProtocolPhaseReceipt.outcomes")
        _validate_sorted_unique([outcome.candidate for outcome in outcomes], "ProtocolPhaseReceipt.outcomes")
        issues = _validate_tuple(self.issues, "ProtocolPhaseReceipt.issues")
        _validate_sorted_unique(issues, "ProtocolPhaseReceipt.issues")
        _validate_sha256(self.artifact_tree_digest, "ProtocolPhaseReceipt.artifact_tree_digest")
        _validate_non_empty_str(self.next_action, "ProtocolPhaseReceipt.next_action")
        if self.status == "pass":
            self._require_canonical_pass(budgets, root_manifests_before, root_manifests_after, write_deltas, outcomes)

    def _require_canonical_pass(
        self,
        budgets: tuple[PhaseBudget, ...],
        root_manifests_before: tuple[RootManifest, ...],
        root_manifests_after: tuple[RootManifest, ...],
        write_deltas: tuple[WriteDelta, ...],
        outcomes: tuple[CandidateProtocolOutcome, ...],
    ) -> None:
        """A ``pass`` receipt must carry a trustworthy, zero-mutation protocol run."""

        if self.issues:
            raise ValueError("ProtocolPhaseReceipt status is pass but it carries issues")
        started = _validate_utc_timestamp(self.started_at, "ProtocolPhaseReceipt.started_at")
        ended = _validate_utc_timestamp(self.ended_at, "ProtocolPhaseReceipt.ended_at")
        if ended < started:
            raise ValueError("ProtocolPhaseReceipt.ended_at must not precede started_at")
        protocol_budget = next((budget for budget in budgets if budget.name == "protocol"), None)
        frozen_protocol_seconds = DEFAULT_PHASE_BUDGETS["protocol"].seconds
        if protocol_budget is None or protocol_budget.seconds != frozen_protocol_seconds:
            raise ValueError(
                "ProtocolPhaseReceipt status is pass but its 'protocol' budget is not the frozen "
                f"{frozen_protocol_seconds} seconds"
            )
        if self.evaluator is None:
            raise ValueError("ProtocolPhaseReceipt status is pass but no evaluator identity is bound")
        if self.runtime_binding is None:
            raise ValueError("ProtocolPhaseReceipt status is pass but no runtime_binding is recorded")
        if self.runtime_binding.lock_digest != self.candidate_lock.digest:
            raise ValueError("ProtocolPhaseReceipt status is pass but runtime_binding names another candidate lock")
        if self.production_identity_before != self.production_identity_after:
            raise ValueError(
                "ProtocolPhaseReceipt status is pass but production identity changed between before and after"
            )
        if not outcomes:
            raise ValueError("ProtocolPhaseReceipt status is pass but it carries no candidate outcome")
        before_by_root = {manifest.root: manifest for manifest in root_manifests_before}
        after_by_root = {manifest.root: manifest for manifest in root_manifests_after}
        delta_roots = {delta.root for delta in write_deltas}
        if not before_by_root:
            raise ValueError("ProtocolPhaseReceipt status is pass but it carries no root manifest")
        if set(before_by_root) != delta_roots or set(after_by_root) != delta_roots:
            raise ValueError(
                "ProtocolPhaseReceipt status is pass but root manifest roots do not match write_deltas roots"
            )
        for delta in write_deltas:
            before_manifest = before_by_root[delta.root]
            after_manifest = after_by_root[delta.root]
            if delta.before_manifest_digest != before_manifest.manifest_digest:
                raise ValueError(
                    f"ProtocolPhaseReceipt status is pass but write_deltas[{delta.root}]."
                    "before_manifest_digest does not match its root_manifests_before entry"
                )
            if delta.after_manifest_digest != after_manifest.manifest_digest:
                raise ValueError(
                    f"ProtocolPhaseReceipt status is pass but write_deltas[{delta.root}]."
                    "after_manifest_digest does not match its root_manifests_after entry"
                )
            if delta.unexpected:
                raise ValueError(
                    f"ProtocolPhaseReceipt status is pass but write_deltas[{delta.root}] has unexpected paths"
                )
            if delta.control_changes:
                raise ValueError(
                    f"ProtocolPhaseReceipt status is pass but write_deltas[{delta.root}] has control_changes"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluation_contract_version": self.evaluation_contract_version,
            "evaluation_identity": self.evaluation_identity,
            "run_identity": self.run_identity,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "budgets": [_phase_budget_to_dict(budget) for budget in self.budgets],
            "evaluator": None if self.evaluator is None else _evaluator_identity_to_dict(self.evaluator),
            "production_identity_before": _production_identity_to_dict(self.production_identity_before),
            "production_identity_after": _production_identity_to_dict(self.production_identity_after),
            "candidate_lock": _candidate_lock_to_dict(self.candidate_lock),
            "runtime_binding": (
                None if self.runtime_binding is None else _runtime_binding_to_dict(self.runtime_binding)
            ),
            "root_manifests_before": [_root_manifest_to_dict(manifest) for manifest in self.root_manifests_before],
            "root_manifests_after": [_root_manifest_to_dict(manifest) for manifest in self.root_manifests_after],
            "write_deltas": [_write_delta_to_dict(delta) for delta in self.write_deltas],
            "outcomes": [_candidate_protocol_outcome_to_dict(outcome) for outcome in self.outcomes],
            "issues": list(self.issues),
            "artifact_tree_digest": self.artifact_tree_digest,
            "next_action": self.next_action,
        }

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> ProtocolPhaseReceipt:
        schema_version = value.get("schema_version")
        if schema_version != PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION:
            raise ValueError(
                f"ProtocolPhaseReceipt schema_version must be {PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION}, "
                f"got {schema_version!r}"
            )
        _closed_fields(value, _PROTOCOL_PHASE_RECEIPT_FIELDS, "ProtocolPhaseReceipt")
        evaluator = value["evaluator"]
        binding = value["runtime_binding"]
        return ProtocolPhaseReceipt(
            schema_version=_expect_int(value["schema_version"], "ProtocolPhaseReceipt.schema_version"),
            evaluation_contract_version=_expect_str(
                value["evaluation_contract_version"], "ProtocolPhaseReceipt.evaluation_contract_version"
            ),
            evaluation_identity=_expect_str(value["evaluation_identity"], "ProtocolPhaseReceipt.evaluation_identity"),
            run_identity=_expect_str(value["run_identity"], "ProtocolPhaseReceipt.run_identity"),
            status=_expect_str(value["status"], "ProtocolPhaseReceipt.status"),
            started_at=_expect_str(value["started_at"], "ProtocolPhaseReceipt.started_at"),
            ended_at=_expect_str(value["ended_at"], "ProtocolPhaseReceipt.ended_at"),
            budgets=tuple(
                _phase_budget_from_dict(item)
                for item in _expect_list(value["budgets"], "ProtocolPhaseReceipt.budgets")
            ),
            evaluator=None if evaluator is None else _evaluator_identity_from_dict(evaluator),
            production_identity_before=_production_identity_from_dict(value["production_identity_before"]),
            production_identity_after=_production_identity_from_dict(value["production_identity_after"]),
            candidate_lock=_candidate_lock_from_dict(value["candidate_lock"]),
            runtime_binding=None if binding is None else _runtime_binding_from_dict(binding),
            root_manifests_before=tuple(
                _root_manifest_from_dict(item)
                for item in _expect_list(value["root_manifests_before"], "ProtocolPhaseReceipt.root_manifests_before")
            ),
            root_manifests_after=tuple(
                _root_manifest_from_dict(item)
                for item in _expect_list(value["root_manifests_after"], "ProtocolPhaseReceipt.root_manifests_after")
            ),
            write_deltas=tuple(
                _write_delta_from_dict(item)
                for item in _expect_list(value["write_deltas"], "ProtocolPhaseReceipt.write_deltas")
            ),
            outcomes=tuple(
                _candidate_protocol_outcome_from_dict(item)
                for item in _expect_list(value["outcomes"], "ProtocolPhaseReceipt.outcomes")
            ),
            issues=tuple(
                _expect_str(item, "ProtocolPhaseReceipt.issues item")
                for item in _expect_list(value["issues"], "ProtocolPhaseReceipt.issues")
            ),
            artifact_tree_digest=_expect_str(
                value["artifact_tree_digest"], "ProtocolPhaseReceipt.artifact_tree_digest"
            ),
            next_action=_expect_str(value["next_action"], "ProtocolPhaseReceipt.next_action"),
        )


_PROTOCOL_PHASE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_contract_version",
        "evaluation_identity",
        "run_identity",
        "status",
        "started_at",
        "ended_at",
        "budgets",
        "evaluator",
        "production_identity_before",
        "production_identity_after",
        "candidate_lock",
        "runtime_binding",
        "root_manifests_before",
        "root_manifests_after",
        "write_deltas",
        "outcomes",
        "issues",
        "artifact_tree_digest",
        "next_action",
    }
)
