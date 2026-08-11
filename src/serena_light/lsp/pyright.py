"""Fixed Pyright facts, configuration, and native-program attribution.

This module deliberately stops before process lifecycle.  The shared adapter
owns starting and stopping the locked command and dispatching LSP messages;
Pyright owns only its language facts, server configuration responder,
capability interpretation, definition-location classification, and the
version-private configured-program projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from serena_light.bootstrap import EXPECTED_VERSIONS, repository_root, runtime_paths
from serena_light.lsp.adapter import (
    AdapterLanguageFacts,
    EngineMetadata,
    SubprocessAdapterRuntimeProvider,
    read_only_client_request_handlers,
)
from serena_light.lsp.normalize import Location, normalize_location
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.python_assignment_recovery import (
    AssignmentRecoveryResult,
    recover_python_module_assignment_symbols,
)
from serena_light.processes import LanguageServerSubprocessLauncher
from serena_light.workspace.identity import (
    MS_INTERPRETER,
    SemanticLocation,
)
from serena_light.workspace.scope import (
    DifferenceReason,
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeProjection,
)

PYRIGHT_EXTENSIONS = frozenset({".py", ".pyi"})
PYRIGHT_LANGUAGE_ID = "python"
PYRIGHT_DEFINITION_METHOD = "textDocument/definition"
PYRIGHT_VERSION = EXPECTED_VERSIONS["pyright"]
_OWNED_FILES_SCHEMA_VERSION = 1


class PyrightConfigurationError(ValueError):
    """Raised when a Pyright server request or initialize result drifts."""


class PyrightAttributionError(RuntimeError):
    """Raised when pinned native-program attribution cannot be proven."""


@dataclass(frozen=True, slots=True)
class RawProviderFacts:
    """Raw initialize providers, kept separate from Serena-compatible tools."""

    definition: bool
    declaration: bool
    implementation: bool
    references: bool
    document_symbols: bool
    workspace_symbols: bool


@dataclass(frozen=True, slots=True)
class DerivedToolFacts:
    """The deliberately smaller Pyright tool surface."""

    find_declaration: bool
    find_implementations: bool
    find_referencing_symbols: bool


@dataclass(frozen=True, slots=True)
class PyrightProviderFacts:
    raw: RawProviderFacts
    derived: DerivedToolFacts


@dataclass(frozen=True, slots=True)
class PyrightDefinitionLocation:
    """One normalized definition plus workspace-policy classification."""

    location: Location
    semantic_location: SemanticLocation


@dataclass(frozen=True, slots=True)
class PyrightOwnedFilesEvidence:
    """Validated output from the pinned AnalyzerService attribution seam."""

    engine_version: str
    cli_entrypoint: Path
    selected_config_path: Path | None
    project_kind: ProjectKind
    owned_files: tuple[Path, ...]
    owned_files_sha256: str
    bundle: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PyrightFacts:
    """Immutable inputs consumed by a later shared adapter implementation."""

    extensions: frozenset[str]
    language_id: str
    command: tuple[str, ...]
    engine_path: Path
    engine_version: str
    interpreter: Path
    definition_method: str = PYRIGHT_DEFINITION_METHOD

    @classmethod
    def locked(
        cls,
        root: Path | None = None,
        *,
        interpreter: Path = MS_INTERPRETER,
    ) -> PyrightFacts:
        project_root = (root or repository_root()).resolve()
        paths = runtime_paths(project_root)
        node = paths["node"]
        language_server = paths["pyright-langserver"]
        cli = paths["pyright"]
        for path in (node, language_server, cli):
            if not path.is_file():
                raise PyrightConfigurationError(f"locked Pyright runtime path is missing: {path}")
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise PyrightConfigurationError(f"selected Python interpreter is missing: {interpreter}")
        return cls(
            extensions=PYRIGHT_EXTENSIONS,
            language_id=PYRIGHT_LANGUAGE_ID,
            command=(str(node), str(language_server), "--stdio"),
            engine_path=cli.resolve(),
            engine_version=PYRIGHT_VERSION,
            # Preserve the exact configured path rather than its python3.12
            # symlink target; this string is part of the adapter contract.
            interpreter=interpreter,
        )

    def initialize_params(self, workspace_root: Path, *, process_id: int | None = None) -> dict[str, Any]:
        """Build native-config-preserving initialize parameters.

        The interpreter is intentionally absent here.  Pyright obtains it only
        from ``workspace/configuration`` after initialize.
        """

        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise PyrightConfigurationError(f"Pyright workspace root is not a directory: {root}")
        root_uri = _path_uri(root)
        return {
            "processId": os.getpid() if process_id is None else process_id,
            "clientInfo": {"name": "serena-light", "version": "0.1.0"},
            "rootPath": str(root),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": root.name}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "symbol": {"dynamicRegistration": True},
                },
                "window": {"workDoneProgress": True},
                "textDocument": {
                    "synchronization": {"dynamicRegistration": True, "didSave": True},
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "publishDiagnostics": {"relatedInformation": True},
                },
            },
            "initializationOptions": {},
            "trace": "off",
        }

    def adapter_language_facts(self, workspace_root: Path) -> AdapterLanguageFacts:
        """Bind fixed Pyright facts to one workspace for the shared core."""

        return AdapterLanguageFacts(
            name="python",
            language_id=self.language_id,
            extensions=self.extensions,
            engine=EngineMetadata(
                name="pyright",
                version=self.engine_version,
                executable=Path(self.command[1]),
                interpreter=self.interpreter,
            ),
            initialize_params=self.initialize_params(workspace_root),
        )

    def runtime_provider(self, workspace_root: Path) -> SubprocessAdapterRuntimeProvider:
        node = Path(self.command[0])
        return SubprocessAdapterRuntimeProvider(
            command=self.command,
            cwd=workspace_root.resolve(strict=True),
            launcher=LanguageServerSubprocessLauncher.get_instance(),
            env={"PATH": str(node.parent), "NODE_PATH": None},
            request_handlers=read_only_client_request_handlers(self.workspace_configuration),
        )

    def workspace_configuration(self, params: Any) -> list[dict[str, Any]]:
        """Answer Pyright's configuration request in item order."""

        if not isinstance(params, Mapping):
            raise PyrightConfigurationError("workspace/configuration params must be an object")
        items = params.get("items")
        if not isinstance(items, Sequence) or isinstance(items, str | bytes):
            raise PyrightConfigurationError("workspace/configuration items must be a sequence")
        answers: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise PyrightConfigurationError("workspace/configuration item must be an object")
            section = item.get("section")
            if section == "python":
                answers.append({"pythonPath": str(self.interpreter)})
            elif section == "python.analysis":
                answers.append(
                    {
                        "diagnosticMode": "workspace",
                        "autoSearchPaths": True,
                        "useLibraryCodeForTypes": True,
                    }
                )
            elif section == "pyright":
                answers.append({})
            else:
                answers.append({})
        return answers

    def provider_facts(self, initialize_result: Mapping[str, Any]) -> PyrightProviderFacts:
        capabilities = initialize_result.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise PyrightConfigurationError("Pyright initialize result has no capabilities object")
        raw = RawProviderFacts(
            definition=_provider_enabled(capabilities.get("definitionProvider")),
            declaration=_provider_enabled(capabilities.get("declarationProvider")),
            implementation=_provider_enabled(capabilities.get("implementationProvider")),
            references=_provider_enabled(capabilities.get("referencesProvider")),
            document_symbols=_provider_enabled(capabilities.get("documentSymbolProvider")),
            workspace_symbols=_provider_enabled(capabilities.get("workspaceSymbolProvider")),
        )
        if not raw.definition:
            raise PyrightConfigurationError("Pyright no longer advertises definitionProvider")
        return PyrightProviderFacts(
            raw=raw,
            derived=DerivedToolFacts(
                find_declaration=raw.definition,
                find_implementations=False,
                find_referencing_symbols=raw.references,
            ),
        )

    def classify_definition_locations(
        self,
        raw_locations: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None,
        *,
        classify: Callable[[Path], SemanticLocation],
    ) -> tuple[PyrightDefinitionLocation, ...]:
        """Normalize definition results through the workspace policy seam."""

        if raw_locations is None:
            return ()
        locations: Sequence[Mapping[str, Any]]
        if isinstance(raw_locations, Mapping):
            locations = (cast(Mapping[str, Any], raw_locations),)
        elif isinstance(raw_locations, Sequence) and not isinstance(raw_locations, str | bytes):
            locations = raw_locations
        else:
            raise PyrightConfigurationError("definition result must be a location, sequence, or null")
        normalized: list[PyrightDefinitionLocation] = []
        for raw in locations:
            if not isinstance(raw, Mapping):
                raise PyrightConfigurationError("definition result contains a non-location entry")
            location = normalize_location(raw)
            if location.path is None:
                raise PyrightConfigurationError(f"Pyright definition is not a local file URI: {location.uri}")
            normalized.append(
                PyrightDefinitionLocation(
                    location=location,
                    semantic_location=classify(Path(location.path)),
                )
            )
        return tuple(normalized)

    def recover_assignment_document_symbols(
        self,
        raw_symbols: Sequence[Mapping[str, Any]] | None,
        *,
        snapshot: FileSnapshot,
        position_encoding: PositionEncoding,
    ) -> AssignmentRecoveryResult:
        """Recover complete module-level assignment ranges for this document.

        Pyright can report a module variable or constant document symbol
        whose ``range`` covers only its identifier.  This adapter-owned seam
        expands that range to the unique enclosing module-level ``Assign`` or
        ``AnnAssign`` statement using the exact verified snapshot, preserving
        the identifier as the selection range.  A symbol that cannot be
        recovered unambiguously keeps its original identifier-only range and
        is reported through the result's ``unresolved`` entries instead of
        being silently expanded.
        """

        return recover_python_module_assignment_symbols(
            raw_symbols,
            snapshot=snapshot,
            position_encoding=position_encoding,
        )

    def attribute_program(
        self,
        workspace_root: Path,
        trust_inventory_paths: Iterable[str],
        *,
        outside_trust_reasons: Mapping[str, DifferenceReason] | None = None,
        timeout: float = 90.0,
    ) -> ScopeProjection:
        """Create a file-level projection from pinned native Pyright evidence."""

        evidence = self.owned_files_evidence(workspace_root, timeout=timeout)
        root = workspace_root.resolve(strict=True)
        relative_paths: list[str] = []
        for path in evidence.owned_files:
            lexical = Path(os.path.abspath(path))
            if lexical.suffix.lower() not in self.extensions:
                continue
            try:
                relative_paths.append(lexical.relative_to(root).as_posix())
            except ValueError as error:
                raise PyrightAttributionError(
                    f"Pyright configured program escaped workspace root: {lexical}"
                ) from error
        selected_config: str | None = None
        if evidence.selected_config_path is not None:
            try:
                selected_config = evidence.selected_config_path.relative_to(root).as_posix()
            except ValueError as error:
                raise PyrightAttributionError(
                    f"Pyright selected config escaped workspace root: {evidence.selected_config_path}"
                ) from error
        attribution = NativeProgramAttribution(
            language=LanguageFamily.PYTHON,
            project_kind=evidence.project_kind,
            selected_config_path=selected_config,
            configured_program_paths=relative_paths,
        )
        return ScopeProjection.from_attribution(
            trust_inventory_paths=trust_inventory_paths,
            attribution=attribution,
            outside_trust_reasons=outside_trust_reasons,
        )

    def owned_files_evidence(self, workspace_root: Path, *, timeout: float = 90.0) -> PyrightOwnedFilesEvidence:
        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise PyrightAttributionError(f"Pyright workspace root is not a directory: {root}")
        node = Path(self.command[0])
        probe = Path(__file__).with_name("pyright_owned_files_probe.mjs")
        command = [
            str(node),
            "--",
            str(probe),
            "--pythonpath",
            str(self.interpreter),
            "-p",
            str(root),
        ]
        environment = os.environ.copy()
        environment["PATH"] = str(node.parent)
        environment.pop("NODE_PATH", None)
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PyrightAttributionError(f"Pyright owned-files attribution failed: {error}") from error
        if completed.returncode:
            detail = (completed.stderr or completed.stdout)[-2000:].strip()
            raise PyrightAttributionError(f"Pyright owned-files attribution exited {completed.returncode}: {detail}")
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise PyrightAttributionError("Pyright owned-files attribution returned invalid JSON") from error
        return _validate_owned_files_report(report, expected_cli=self.engine_path)


def _validate_owned_files_report(report: Any, *, expected_cli: Path) -> PyrightOwnedFilesEvidence:
    if not isinstance(report, Mapping) or report.get("schema_version") != _OWNED_FILES_SCHEMA_VERSION:
        raise PyrightAttributionError("Pyright owned-files attribution returned an unsupported schema")
    engine = report.get("engine")
    project = report.get("project")
    owned = report.get("owned_files")
    bundle = report.get("bundle")
    if (
        not isinstance(engine, Mapping)
        or not isinstance(project, Mapping)
        or not isinstance(owned, list)
        or not isinstance(bundle, Mapping)
    ):
        raise PyrightAttributionError("Pyright owned-files attribution omitted required evidence")
    version = engine.get("version")
    if version != PYRIGHT_VERSION:
        raise PyrightAttributionError(f"Pyright version drift: {version!r} != {PYRIGHT_VERSION!r}")
    cli = Path(str(engine.get("cli_entrypoint"))).resolve()
    if cli != expected_cli.resolve():
        raise PyrightAttributionError(f"Pyright CLI attribution drift: {cli} != {expected_cli.resolve()}")
    paths: list[Path] = []
    for raw in owned:
        if not isinstance(raw, str) or not Path(raw).is_absolute() or "\0" in raw:
            raise PyrightAttributionError(f"Pyright owned-files attribution contains an invalid path: {raw!r}")
        paths.append(Path(raw))
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise PyrightAttributionError("Pyright owned-files attribution paths are not unique and sorted")
    digest = _path_digest(paths)
    if report.get("owned_file_count") != len(paths) or report.get("owned_files_sha256") != digest:
        raise PyrightAttributionError("Pyright owned-files count or digest does not match path evidence")
    project_kind_raw = project.get("project_kind")
    try:
        project_kind = ProjectKind(project_kind_raw)
    except ValueError as error:
        raise PyrightAttributionError(f"invalid Pyright project kind: {project_kind_raw!r}") from error
    raw_config = project.get("selected_config_path")
    selected_config = None if raw_config is None else Path(str(raw_config))
    if selected_config is not None and (not selected_config.is_absolute() or "\0" in str(selected_config)):
        raise PyrightAttributionError("Pyright selected config path is invalid")
    if (project_kind is ProjectKind.CONFIGURED) is (selected_config is None):
        raise PyrightAttributionError("Pyright project kind and selected config are inconsistent")
    return PyrightOwnedFilesEvidence(
        engine_version=version,
        cli_entrypoint=cli,
        selected_config_path=selected_config,
        project_kind=project_kind,
        owned_files=tuple(paths),
        owned_files_sha256=digest,
        bundle=bundle,
    )


def _provider_enabled(value: Any) -> bool:
    return value is True or isinstance(value, Mapping)


def _path_uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _path_digest(paths: Iterable[Path]) -> str:
    return hashlib.sha256("\0".join(str(path) for path in paths).encode("utf-8", "surrogateescape")).hexdigest()
