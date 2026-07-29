"""Fixed TypeScript adapter facts and native tsserver scope attribution.

The shared adapter lifecycle intentionally owns the long-lived
``typescript-language-server`` process.  This module owns only TypeScript's
fixed routing/initialization contract and a bounded direct-tsserver
``projectInfo`` probe, because the LSP does not expose the configured program's
file list.
"""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import stat
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import quote

from serena_light.bootstrap import EXPECTED_VERSIONS, inspect_runtime, repository_root
from serena_light.lsp.adapter import (
    AdapterLanguageFacts,
    EngineMetadata,
    SubprocessAdapterRuntimeProvider,
    read_only_client_request_handlers,
)
from serena_light.processes import LanguageServerSubprocessLauncher, terminate_process_tree_with_kill_fallback
from serena_light.workspace.scope import (
    DifferenceReason,
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeProjection,
    bounded_difference_status,
)

TYPESCRIPT_LANGUAGE_SERVER_VERSION = EXPECTED_VERSIONS["typescript-language-server"]
TYPESCRIPT_VERSION = EXPECTED_VERSIONS["typescript"]
TYPESCRIPT_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
LANGUAGE_IDS: Mapping[str, str] = MappingProxyType(
    {
        ".js": "javascript",
        ".jsx": "javascriptreact",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".mts": "typescript",
        ".cts": "typescript",
    }
)
NATIVE_CONFIG_NAMES = frozenset({"tsconfig.json", "jsconfig.json"})
DEFAULT_POSITION_ENCODING = "utf-16"
PROJECT_INFO_TIMEOUT_SECONDS = 10.0


class TypeScriptAdapterError(RuntimeError):
    """Raised when fixed adapter facts or engine attribution are unusable."""


class TypeScriptScopeError(TypeScriptAdapterError):
    """Typed failure for a native program that crosses the trust boundary."""

    code = "SCOPE_INCOMPATIBLE"

    def __init__(self, paths: Iterable[str], message: str | None = None) -> None:
        self.paths = tuple(sorted(set(paths)))
        detail = message or "TypeScript configured program contains paths outside trust"
        super().__init__(f"{self.code}: {detail}: {list(self.paths)!r}")


@dataclass(frozen=True, slots=True)
class RejectedProgramPath:
    """A tsserver-attributed path rejected before semantic projection."""

    path: str
    reason: DifferenceReason


@dataclass(frozen=True, slots=True)
class TypeScriptCapabilityFacts:
    """Raw initialize providers and the tools derived from those providers."""

    raw_providers: Mapping[str, bool]
    derived_tools: Mapping[str, bool]
    position_encoding: str

    @classmethod
    def from_initialize_result(cls, result: Mapping[str, Any]) -> Self:
        capabilities = result.get("capabilities", result)
        if not isinstance(capabilities, Mapping):
            raise TypeScriptAdapterError("initialize result has no capability mapping")
        raw = {
            name: _provider_enabled(capabilities.get(name))
            for name in (
                "definitionProvider",
                "declarationProvider",
                "implementationProvider",
                "referencesProvider",
                "documentSymbolProvider",
                "workspaceSymbolProvider",
            )
        }
        derived = {
            "find_declaration": raw["definitionProvider"],
            "find_implementations": raw["implementationProvider"],
            "find_referencing_symbols": raw["referencesProvider"],
            "get_symbols_overview": raw["documentSymbolProvider"],
            "find_symbol_global": raw["workspaceSymbolProvider"],
        }
        selected = capabilities.get("positionEncoding", DEFAULT_POSITION_ENCODING)
        if selected not in {"utf-8", "utf-16", "utf-32"}:
            raise TypeScriptAdapterError(f"unsupported server position encoding: {selected!r}")
        return cls(MappingProxyType(raw), MappingProxyType(derived), str(selected))


@dataclass(frozen=True, slots=True)
class TypeScriptAdapterConfig:
    """Repository-locked facts callable by the future shared adapter seam."""

    node_path: Path
    language_server_path: Path
    tsserver_path: Path
    language_server_version: str
    typescript_version: str
    lock_digest: str

    @classmethod
    def locked(cls, root: Path | None = None) -> Self:
        status = inspect_runtime((root or repository_root()).resolve())
        paths = status["paths"]
        versions = status["versions"]
        return cls(
            node_path=Path(paths["node"]),
            language_server_path=Path(paths["typescript-language-server"]),
            tsserver_path=Path(paths["tsserver"]),
            language_server_version=str(versions["typescript-language-server"]),
            typescript_version=str(versions["typescript"]),
            lock_digest=str(status["lock_digest"]),
        )

    def __post_init__(self) -> None:
        if self.language_server_version != TYPESCRIPT_LANGUAGE_SERVER_VERSION:
            raise TypeScriptAdapterError(
                f"typescript-language-server drift: {self.language_server_version} != "
                f"{TYPESCRIPT_LANGUAGE_SERVER_VERSION}"
            )
        if self.typescript_version != TYPESCRIPT_VERSION:
            raise TypeScriptAdapterError(f"TypeScript drift: {self.typescript_version} != {TYPESCRIPT_VERSION}")
        for name, path in (
            ("node", self.node_path),
            ("typescript-language-server", self.language_server_path),
            ("tsserver", self.tsserver_path),
        ):
            if not path.is_absolute() or not path.is_file():
                raise TypeScriptAdapterError(f"locked {name} path is not an absolute file: {path}")
        expected_tsserver = self.language_server_path.parents[2] / "typescript" / "lib" / "tsserver.js"
        if self.tsserver_path != expected_tsserver:
            raise TypeScriptAdapterError(
                "TypeScript engine is not server-owned beside the locked language server: "
                f"{self.tsserver_path} != {expected_tsserver}"
            )

    @property
    def extensions(self) -> tuple[str, ...]:
        return TYPESCRIPT_EXTENSIONS

    @property
    def language_ids(self) -> Mapping[str, str]:
        return LANGUAGE_IDS

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.node_path), str(self.language_server_path), "--stdio")

    def language_id(self, path: str | Path) -> str:
        suffix = Path(path).suffix.lower()
        try:
            return LANGUAGE_IDS[suffix]
        except KeyError as error:
            raise TypeScriptAdapterError(f"unsupported TypeScript-family extension: {suffix!r}") from error

    def initialize_params(self, workspace_root: Path, *, process_id: int | None = None) -> dict[str, Any]:
        root = workspace_root.resolve(strict=True)
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
                    "implementation": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "publishDiagnostics": {"relatedInformation": True},
                },
            },
            "initializationOptions": {
                "preferences": {"disableAutomaticTypingAcquisition": True},
                "tsserver": {"path": str(self.tsserver_path)},
            },
            "trace": "off",
        }

    def adapter_language_facts(self, workspace_root: Path) -> AdapterLanguageFacts:
        """Bind the fixed TypeScript family to one shared adapter instance."""

        return AdapterLanguageFacts(
            name="typescript",
            language_id="typescript",
            extensions=frozenset(self.extensions),
            language_ids=self.language_ids,
            engine=EngineMetadata(
                name="typescript-language-server",
                version=self.language_server_version,
                executable=self.language_server_path,
            ),
            initialize_params=self.initialize_params(workspace_root),
        )

    @staticmethod
    def workspace_configuration(params: Any) -> list[dict[str, Any]]:
        if not isinstance(params, Mapping):
            raise TypeScriptAdapterError("workspace/configuration params must be an object")
        items = params.get("items")
        if not isinstance(items, list):
            raise TypeScriptAdapterError("workspace/configuration items must be a list")
        return [{} for _item in items]

    def runtime_provider(self, workspace_root: Path) -> SubprocessAdapterRuntimeProvider:
        return SubprocessAdapterRuntimeProvider(
            command=self.command,
            cwd=workspace_root.resolve(strict=True),
            launcher=LanguageServerSubprocessLauncher.get_instance(),
            env={"PATH": str(self.node_path.parent), "NODE_PATH": None},
            request_handlers=read_only_client_request_handlers(self.workspace_configuration),
        )

    def fixed_facts(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "adapter": "typescript",
                "extensions": self.extensions,
                "language_ids": self.language_ids,
                "command": self.command,
                "language_server_path": str(self.language_server_path),
                "language_server_version": self.language_server_version,
                "typescript_engine_path": str(self.tsserver_path),
                "typescript_engine_version": self.typescript_version,
                "lock_digest": self.lock_digest,
                "diagnostic_authority": "advisory",
            }
        )

    def capability_facts(self, initialize_result: Mapping[str, Any]) -> TypeScriptCapabilityFacts:
        return TypeScriptCapabilityFacts.from_initialize_result(initialize_result)


@dataclass(frozen=True, slots=True)
class TypeScriptScopeAttribution:
    """Native configured-program projection plus fail-closed path rejections."""

    entry_path: str
    projection: ScopeProjection
    rejected_configured_paths: tuple[RejectedProgramPath, ...]

    @property
    def scope_compatible(self) -> bool:
        return self.projection.compatible and not self.rejected_configured_paths

    def require_compatible(self) -> ScopeProjection:
        if self.scope_compatible:
            return self.projection
        paths = [difference.path for difference in self.projection.configured_program_outside_trust]
        paths.extend(rejected.path for rejected in self.rejected_configured_paths)
        raise TypeScriptScopeError(paths)

    def status_facts(self) -> Mapping[str, Any]:
        projection = self.projection
        return MappingProxyType(
            {
                "selected_config_path": projection.selected_config_path,
                "project_kind": projection.project_kind.value,
                "trust_inventory_count": projection.trust_inventory.count,
                "trust_inventory_sha256": projection.trust_inventory.sha256,
                "configured_program_count": projection.configured_program.count,
                "configured_program_sha256": projection.configured_program.sha256,
                "trusted_not_in_configured_program": bounded_difference_status(
                    projection.trusted_not_in_configured_program
                ),
                "configured_program_outside_trust": bounded_difference_status(
                    projection.configured_program_outside_trust
                ),
                "configured_program_rejected": tuple(
                    {"path": item.path, "reason": item.reason.value} for item in self.rejected_configured_paths
                ),
                "scope_compatible": self.scope_compatible,
                "overlay_generated": False,
            }
        )


@dataclass(frozen=True, slots=True)
class InferredPathSupport:
    """Evidence that one omitted trusted path stays in an inferred project."""

    path: str
    project_kind: str
    selected_config_path: str | None
    engine_owned: bool
    service_supported: bool
    configured_program_unchanged: bool
    configured_program_before: tuple[str, ...]
    configured_program_after: tuple[str, ...]
    inferred_program: tuple[str, ...]

    @property
    def supported_without_scope_expansion(self) -> bool:
        return self.engine_owned and self.service_supported and self.configured_program_unchanged


def attribute_native_program(
    config: TypeScriptAdapterConfig,
    workspace_root: Path,
    *,
    trust_inventory_paths: Iterable[str],
    entry_path: str | Path,
    timeout: float = PROJECT_INFO_TIMEOUT_SECONDS,
) -> TypeScriptScopeAttribution:
    """Ask the pinned tsserver for its native program and compare path sets."""

    root = workspace_root.resolve(strict=True)
    entry = _trusted_source(root, entry_path)
    with _TsServerClient(config, root, timeout=timeout) as client:
        client.command("open", {"file": str(entry), "projectRootPath": str(root)}, wait=False)
        response = client.command("projectInfo", {"file": str(entry), "needFileNameList": True})
    body = _successful_body("projectInfo", response)
    return project_info_to_scope(
        root,
        trust_inventory_paths=trust_inventory_paths,
        entry_path=entry.relative_to(root).as_posix(),
        project_info_body=body,
        engine_library_dir=config.tsserver_path.resolve(strict=True).parent,
    )


def project_info_to_scope(
    workspace_root: Path,
    *,
    trust_inventory_paths: Iterable[str],
    entry_path: str,
    project_info_body: Mapping[str, Any],
    engine_library_dir: Path | None = None,
) -> TypeScriptScopeAttribution:
    """Pure normalization seam for direct-tsserver ``projectInfo`` output."""

    root = workspace_root.resolve(strict=True)
    selected_config, project_kind = _selected_config(root, project_info_body.get("configFileName"))
    raw_file_names = project_info_body.get("fileNames", ())
    if not isinstance(raw_file_names, list) or not all(isinstance(path, str) for path in raw_file_names):
        raise TypeScriptAdapterError("projectInfo body has no string fileNames list")
    trust_paths = tuple(trust_inventory_paths)
    program, rejected = _normalize_program_paths(root, raw_file_names, engine_library_dir=engine_library_dir)
    attribution = NativeProgramAttribution(
        language=LanguageFamily.TYPESCRIPT,
        project_kind=project_kind,
        selected_config_path=selected_config,
        configured_program_paths=program,
    )
    outside_reasons = dict.fromkeys(set(program) - set(trust_paths), DifferenceReason.ABSENT_FROM_GIT_TRUST_INVENTORY)
    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=trust_paths,
        attribution=attribution,
        outside_trust_reasons=outside_reasons,
    )
    return TypeScriptScopeAttribution(entry_path, projection, rejected)


_NATIVE_CONFIG_CANDIDATES: tuple[str, ...] = ("tsconfig.json", "jsconfig.json")


def select_default_entry(root: Path, paths: Iterable[str]) -> str:
    """Prefer a path the root native config's include/exclude patterns claim."""

    ordered = tuple(sorted(paths))
    if not ordered:
        raise TypeScriptAdapterError("no TypeScript-family paths available for default entry selection")
    include, exclude = _root_native_config_patterns(root)
    for candidate in ordered:
        if any(_matches_native_pattern(candidate, pattern) for pattern in exclude):
            continue
        if include is None or any(_matches_native_pattern(candidate, pattern) for pattern in include):
            return candidate
    return ordered[0]


def _root_native_config_patterns(root: Path) -> tuple[list[str] | None, list[str]]:
    for name in _NATIVE_CONFIG_CANDIDATES:
        candidate = root / name
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, []
        include = data.get("include") if isinstance(data, Mapping) else None
        exclude = data.get("exclude") if isinstance(data, Mapping) else None
        return (
            [item for item in include if isinstance(item, str)] if isinstance(include, list) else None,
            [item for item in exclude if isinstance(item, str)] if isinstance(exclude, list) else [],
        )
    return None, []


def _matches_native_pattern(path: str, pattern: str) -> bool:
    normalized = pattern.strip("/")
    if not normalized:
        return False
    if path == normalized or path.startswith(f"{normalized}/"):
        return True
    return fnmatch.fnmatch(path, normalized) or fnmatch.fnmatch(path, normalized.replace("**/", ""))


def probe_inferred_path_support(
    config: TypeScriptAdapterConfig,
    workspace_root: Path,
    *,
    configured_entry_path: str | Path,
    candidate_path: str | Path,
    timeout: float = PROJECT_INFO_TIMEOUT_SECONDS,
) -> InferredPathSupport:
    """Prove an omitted file is served without expanding the configured program."""

    root = workspace_root.resolve(strict=True)
    configured_entry = _trusted_source(root, configured_entry_path)
    candidate = _trusted_source(root, candidate_path)
    candidate_relative = candidate.relative_to(root).as_posix()
    with _TsServerClient(config, root, timeout=timeout) as client:
        client.command("open", {"file": str(configured_entry), "projectRootPath": str(root)}, wait=False)
        before = _successful_body(
            "projectInfo",
            client.command("projectInfo", {"file": str(configured_entry), "needFileNameList": True}),
        )
        client.command("open", {"file": str(candidate), "projectRootPath": str(root)}, wait=False)
        candidate_info = _successful_body(
            "projectInfo",
            client.command("projectInfo", {"file": str(candidate), "needFileNameList": True}),
        )
        service = _successful_body("navtree", client.command("navtree", {"file": str(candidate)}))
        after = _successful_body(
            "projectInfo",
            client.command("projectInfo", {"file": str(configured_entry), "needFileNameList": True}),
        )

    before_paths, before_rejected = _normalize_program_paths(root, _string_file_names(before))
    after_paths, after_rejected = _normalize_program_paths(root, _string_file_names(after))
    inferred_paths, inferred_rejected = _normalize_program_paths(root, _string_file_names(candidate_info))
    selected_config, _ = _selected_config(root, candidate_info.get("configFileName"))
    inferred = selected_config is None
    unchanged = (
        before_paths == after_paths
        and before_rejected == after_rejected
        and before.get("configFileName") == after.get("configFileName")
    )
    service_supported = isinstance(service, Mapping) and candidate_relative in inferred_paths and not inferred_rejected
    return InferredPathSupport(
        path=candidate_relative,
        project_kind="inferred" if inferred else "configured",
        selected_config_path=selected_config,
        engine_owned=inferred,
        service_supported=service_supported,
        configured_program_unchanged=unchanged,
        configured_program_before=before_paths,
        configured_program_after=after_paths,
        inferred_program=inferred_paths,
    )


class _TsServerClient:
    """Minimal bounded direct-tsserver client used only for ``projectInfo``."""

    def __init__(self, config: TypeScriptAdapterConfig, cwd: Path, *, timeout: float) -> None:
        if timeout <= 0 or timeout > 30:
            raise ValueError("direct tsserver timeout must be in (0, 30] seconds")
        self._timeout = timeout
        self._sequence = 0
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._process = LanguageServerSubprocessLauncher.get_instance().launch(
            (str(config.node_path), str(config.tsserver_path)),
            cwd=cwd,
            env={"PATH": str(config.node_path.parent)},
        )
        self._reader = threading.Thread(target=self._read_loop, name="serena-light-tsserver-project-info", daemon=True)
        self._reader.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _read_loop(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        while True:
            line = stdout.readline()
            if not line:
                return
            if not line.lower().startswith(b"content-length:"):
                continue
            try:
                length = int(line.partition(b":")[2].strip())
                while stdout.readline() not in {b"\r\n", b"\n", b""}:
                    pass
                payload = json.loads(stdout.read(length))
            except (ValueError, json.JSONDecodeError):
                return
            if isinstance(payload, dict) and payload.get("type") == "response":
                self._responses.put(payload)

    def command(self, name: str, arguments: Mapping[str, Any], *, wait: bool = True) -> dict[str, Any] | None:
        self._sequence += 1
        sequence = self._sequence
        request = {"seq": sequence, "type": "request", "command": name, "arguments": dict(arguments)}
        stdin = self._process.stdin
        if stdin is None or self._process.poll() is not None:
            raise TypeScriptAdapterError("direct tsserver process is not running")
        stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        stdin.flush()
        if not wait:
            return None
        try:
            while True:
                response = self._responses.get(timeout=self._timeout)
                if response.get("request_seq") == sequence:
                    return response
        except queue.Empty as error:
            raise TimeoutError(f"tsserver did not answer {name!r} within {self._timeout:g}s") from error

    def close(self) -> None:
        terminate_process_tree_with_kill_fallback(
            self._process,
            2.0,
            "direct tsserver projectInfo probe",
            kill_timeout=2.0,
        )
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()
        self._reader.join(timeout=1.0)


def _provider_enabled(value: Any) -> bool:
    return value is True or isinstance(value, Mapping)


def _path_uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _trusted_source(root: Path, supplied: str | Path) -> Path:
    raw = Path(supplied)
    lexical = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.normpath(lexical))
    try:
        relative = lexical.relative_to(root)
        mode = lexical.lstat().st_mode
        resolved = lexical.resolve(strict=True)
    except (ValueError, FileNotFoundError, OSError, RuntimeError) as error:
        raise TypeScriptAdapterError(f"invalid TypeScript source path: {supplied}") from error
    if relative.suffix.lower() not in TYPESCRIPT_EXTENSIONS:
        raise TypeScriptAdapterError(f"unsupported TypeScript source path: {relative.as_posix()}")
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or resolved != lexical:
        raise TypeScriptAdapterError(f"TypeScript source is not a lexical in-root regular file: {relative.as_posix()}")
    return lexical


def _selected_config(root: Path, raw: Any) -> tuple[str | None, ProjectKind]:
    if not isinstance(raw, str) or not raw:
        return None, ProjectKind.WORKSPACE_DEFAULT
    path = Path(raw)
    if path.name not in NATIVE_CONFIG_NAMES or not path.is_file():
        return None, ProjectKind.WORKSPACE_DEFAULT
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise TypeScriptScopeError((str(resolved),), "tsserver selected a native config outside the workspace")
    return resolved.relative_to(root).as_posix(), ProjectKind.CONFIGURED


def _normalize_program_paths(
    root: Path, raw_paths: Iterable[str], *, engine_library_dir: Path | None = None
) -> tuple[tuple[str, ...], tuple[RejectedProgramPath, ...]]:
    accepted: set[str] = set()
    rejected: set[RejectedProgramPath] = set()
    for raw in raw_paths:
        supplied = Path(raw)
        rooted = supplied if supplied.is_absolute() else root / supplied
        claims_root = not supplied.is_absolute() or rooted.is_relative_to(root)
        lexical = Path(os.path.normpath(rooted))
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            if claims_root and supplied.suffix.lower() in TYPESCRIPT_EXTENSIONS:
                rejected.add(RejectedProgramPath(supplied.as_posix(), DifferenceReason.OUTSIDE_WORKSPACE))
            continue
        normalized = relative.as_posix()
        if normalized == "node_modules" or normalized.startswith("node_modules/"):
            continue
        if relative.suffix.lower() not in TYPESCRIPT_EXTENSIONS:
            continue
        try:
            mode = lexical.lstat().st_mode
            resolved = lexical.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            rejected.add(RejectedProgramPath(normalized, DifferenceReason.MISSING))
            continue
        if engine_library_dir is not None and resolved.parent == engine_library_dir:
            # The pinned engine's own default-library declarations (lib*.d.ts,
            # alongside tsserver.js) are implicitly part of every program; they
            # are not repository source and never require Git trust.
            continue
        if stat.S_ISLNK(mode) or resolved != lexical:
            rejected.add(RejectedProgramPath(normalized, DifferenceReason.SYMLINK_OR_ESCAPE))
        elif not stat.S_ISREG(mode):
            rejected.add(RejectedProgramPath(normalized, DifferenceReason.NON_REGULAR))
        else:
            accepted.add(normalized)
    return tuple(sorted(accepted)), tuple(sorted(rejected, key=lambda item: (item.path, item.reason.value)))


def _successful_body(command: str, response: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not response or response.get("success") is not True or not isinstance(response.get("body"), Mapping):
        raise TypeScriptAdapterError(f"tsserver {command} failed: {response!r}")
    return response["body"]


def _string_file_names(body: Mapping[str, Any]) -> list[str]:
    file_names = body.get("fileNames")
    if not isinstance(file_names, list) or not all(isinstance(path, str) for path in file_names):
        raise TypeScriptAdapterError("projectInfo body has no string fileNames list")
    return file_names
