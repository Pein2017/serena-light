"""Private disposable behavior witness for the Phase 2 protocol gate.

This module is evaluation-only.  It adds no MCP or product schema.  One call creates one
fixed Python document below a caller-owned root, runs exactly one protocol session, records
bounded semantic/configuration evidence, proves the document was not changed, and removes
the disposable document.  The caller remains responsible for the outer phase manifest and
for admitting the immutable candidate runtime.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from scripts.backend_eval.manifests import MS_TRANSFORMERS_ROOT
from scripts.backend_eval.models import (
    PROTOCOL_WITNESS_SCHEMA_VERSION,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.process import Deadline, DeadlineExceeded
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    ProtocolSession,
    protocol_session_from_error,
    redacted_evidence_text,
    run_protocol_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime
from serena_light.lsp.adapter import RawLspProviders
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.normalize import Location, NormalizationError, normalize_location
from serena_light.lsp.positions import (
    FileSnapshot,
    LspPosition,
    PositionEncoding,
    PositionError,
    PublicPositionRenderer,
)

__all__ = [
    "FIXTURE_BYTES",
    "PROTOCOL_WITNESS_SCHEMA_VERSION",
    "ProtocolBehaviorWitness",
    "ProtocolWitnessRequest",
    "ProtocolWitnessSetupError",
    "run_protocol_behavior_witness",
]

FIXTURE_BYTES = (
    b"from definitely_missing_serena_light_witness import MissingWitness\n"
    b"from transformers import GenerationConfig\n"
    b"\n"
    b"def build_config() -> GenerationConfig:\n"
    b'    marker = "\xf0\x9f\xa7\xaa"; y = GenerationConfig()\n'
    b"    reveal = MissingWitness\n"
    b"    return y\n"
)

_CANDIDATES = frozenset({"pyright", "ty", "pyrefly"})
_FIXTURE_DIRECTORY_PREFIX = "protocol-witness-"
_FIXTURE_NAME = "witness.py"
_MISSING_IMPORT = "definitely_missing_serena_light_witness"
_PUSH_DIAGNOSTICS_WAIT_SECONDS = 10.0
_MAX_ISSUES = 16
_MAX_ISSUE_CHARS = 256


class ProtocolWitnessSetupError(RuntimeError):
    """The disposable fixture could not be created without overwriting owned state."""


class _ProbeRunner(Protocol):
    def __call__(
        self,
        spec: BackendProtocolSpec,
        runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Callable[[SyncLspClient, RawLspProviders], object],
    ) -> ProtocolSession[object]: ...


@dataclass(frozen=True, slots=True)
class ProtocolWitnessRequest:
    """Already-admitted inputs for one candidate's one disposable witness session."""

    candidate: str
    spec: BackendProtocolSpec
    runtime: CandidateRuntime
    owned_root: Path
    owned_root_fd: int | None = None

    def __post_init__(self) -> None:
        if self.candidate not in _CANDIDATES:
            raise ValueError(
                f"ProtocolWitnessRequest.candidate must be one of {sorted(_CANDIDATES)}"
            )
        if self.spec.name != self.candidate:
            raise ValueError("ProtocolWitnessRequest.spec.name must equal candidate")
        if not self.owned_root.is_absolute():
            raise ValueError("ProtocolWitnessRequest.owned_root must be absolute")
        run_name = self.owned_root.name
        if (
            len(run_name) != 64
            or run_name != run_name.lower().strip()
            or any(character not in "0123456789abcdef" for character in run_name)
        ):
            raise ValueError(
                "ProtocolWitnessRequest.owned_root must be a canonical per-run root "
                "named by its 64-character run identity"
            )
        if self.owned_root_fd is not None and (
            isinstance(self.owned_root_fd, bool)
            or not isinstance(self.owned_root_fd, int)
            or self.owned_root_fd < 0
        ):
            raise ValueError(
                "ProtocolWitnessRequest.owned_root_fd must be one open descriptor or None"
            )


@dataclass(frozen=True, slots=True)
class ProtocolBehaviorWitness:
    """Bounded, canonical-sidecar-ready evidence from one behavior witness."""

    schema_version: int
    candidate: str
    passed: bool
    fixture_sha256: str
    fixture_mode: int
    fixture_unchanged: bool
    selected_interpreter: str
    configuration_transport: str
    configuration_interpreter: str | None
    configuration_path: str | None
    configuration_payload_sha256: str | None
    configuration_request_count: int
    configuration_application_proven: bool
    external_definition_relative_path: str | None
    position_encoding: str
    y_raw_range: tuple[int, int, int, int] | None
    y_decoded_range: tuple[int, int, int, int] | None
    push_diagnostics_claimed: bool
    exact_uri_diagnostics: bool
    missing_import_diagnostic: bool
    exact_uri_publish_count: int
    exact_uri_diagnostic_count: int
    diagnostics_completion_reason: str
    first_normalized_capability: str | None
    first_normalized_count: int
    first_readiness_seconds: float | None
    raw_providers: tuple[tuple[str, bool], ...]
    terminal_error_count: int
    cleanup_error_count: int
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        raw_range: dict[str, object] | None = None
        if self.y_raw_range is not None:
            start_line, start_character, end_line, end_character = self.y_raw_range
            unit = {
                PositionEncoding.UTF8.value: "utf8_byte",
                PositionEncoding.UTF16.value: "utf16_code_unit",
                PositionEncoding.UTF32.value: "unicode_code_point",
            }[self.position_encoding]
            raw_range = {
                "basis": f"lsp_zero_based_line_{unit}_character",
                "range": [[start_line, start_character], [end_line, end_character]],
            }
        decoded_range = (
            None
            if self.y_decoded_range is None
            else [
                [self.y_decoded_range[0], self.y_decoded_range[1]],
                [self.y_decoded_range[2], self.y_decoded_range[3]],
            ]
        )
        return {
            "schema_version": self.schema_version,
            "candidate": self.candidate,
            "passed": self.passed,
            "fixture": {
                "sha256": self.fixture_sha256,
                "mode": self.fixture_mode,
                "unchanged": self.fixture_unchanged,
            },
            "configuration": {
                "transport": self.configuration_transport,
                "selected_interpreter": self.selected_interpreter,
                "observed_interpreter": self.configuration_interpreter,
                "observed_config_path": self.configuration_path,
                "payload_sha256": self.configuration_payload_sha256,
                "server_request_count": self.configuration_request_count,
                "application_proven": self.configuration_application_proven,
            },
            "external_definition": {
                "frozen_root": str(MS_TRANSFORMERS_ROOT),
                "relative_path": self.external_definition_relative_path,
            },
            "position": {
                "encoding": self.position_encoding,
                "y_raw": raw_range,
                "y_decoded_code_points": decoded_range,
            },
            "diagnostics": {
                "push_claimed": self.push_diagnostics_claimed,
                "exact_uri_observed": self.exact_uri_diagnostics,
                "missing_import_observed": self.missing_import_diagnostic,
                "exact_uri_publish_count": self.exact_uri_publish_count,
                "exact_uri_diagnostic_count": self.exact_uri_diagnostic_count,
                "completion_reason": self.diagnostics_completion_reason,
            },
            "readiness": {
                "first_normalized_capability": self.first_normalized_capability,
                "normalized_count": self.first_normalized_count,
                "elapsed_seconds": self.first_readiness_seconds,
            },
            "raw_providers": dict(self.raw_providers),
            "terminal_error_count": self.terminal_error_count,
            "cleanup_error_count": self.cleanup_error_count,
            "issues": list(self.issues),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


@dataclass(slots=True)
class _ConfigurationObservation:
    initialize_params: Mapping[str, object] | None = None
    request_payloads: list[tuple[object, object]] | None = None
    sent_change_configuration: object | None = None

    def __post_init__(self) -> None:
        if self.request_payloads is None:
            self.request_payloads = []


@dataclass(slots=True)
class _DiagnosticsObservation:
    fixture_uri: str
    document_version: int | None = None
    exact_uri_observed: bool = False
    missing_import_observed: bool = False
    exact_uri_count: int = 0
    diagnostic_count: int = 0
    event: threading.Event | None = None

    def __post_init__(self) -> None:
        if self.event is None:
            self.event = threading.Event()

    def arm(self, *, document_version: int) -> None:
        if (
            isinstance(document_version, bool)
            or not isinstance(document_version, int)
            or document_version < 0
        ):
            raise ValueError("diagnostics document version must be a non-negative integer")
        if self.document_version is not None:
            raise ValueError("diagnostics observation is already armed")
        self.document_version = document_version

    def observe(self, method: str, params: Any) -> None:
        if method != "textDocument/publishDiagnostics" or not isinstance(params, Mapping):
            return
        if self.document_version is None:
            return
        if params.get("uri") != self.fixture_uri:
            return
        if "version" in params:
            published_version = params.get("version")
            if (
                isinstance(published_version, bool)
                or not isinstance(published_version, int)
                or published_version != self.document_version
            ):
                return
        diagnostics = params.get("diagnostics")
        if not isinstance(diagnostics, Sequence) or isinstance(diagnostics, str | bytes):
            return
        self.exact_uri_observed = True
        self.exact_uri_count += 1
        self.diagnostic_count += len(diagnostics)
        for item in diagnostics:
            if isinstance(item, Mapping) and _MISSING_IMPORT in str(item.get("message", "")):
                self.missing_import_observed = True
        if self.missing_import_observed:
            assert self.event is not None
            self.event.set()


@dataclass(frozen=True, slots=True)
class _SessionResult:
    external_definition: Location | None
    local_y_definition: Location | None
    first_normalized_count: int
    first_readiness_seconds: float | None


@dataclass(slots=True)
class _DisposableFixture:
    owned_root: Path
    directory_name: str
    directory_path: Path
    root_fd: int
    directory_fd: int
    directory_identity: tuple[int, int]

    @classmethod
    def create(
        cls,
        owned_root: Path,
        candidate: str,
        *,
        deadline: Deadline,
        owned_root_fd: int | None = None,
    ) -> _DisposableFixture:
        try:
            root_fd = (
                _open_owned_run_root(owned_root, deadline=deadline)
                if owned_root_fd is None
                else _duplicate_owned_run_root(owned_root_fd, deadline=deadline)
            )
        except OSError as error:
            raise ProtocolWitnessSetupError(
                f"could not open caller-owned witness root: {type(error).__name__}"
            ) from error
        directory_name = f"{_FIXTURE_DIRECTORY_PREFIX}{candidate}"
        directory_fd = -1
        created = False
        try:
            try:
                _checked_call(
                    deadline,
                    "protocol witness fixture mkdir",
                    lambda: os.mkdir(directory_name, mode=0o700, dir_fd=root_fd),
                )
                created = True
            except FileExistsError as error:
                raise ProtocolWitnessSetupError(
                    f"disposable witness directory already exists: {directory_name}"
                ) from error
            _checked_call(
                deadline,
                "protocol witness fixture directory chmod",
                lambda: os.chmod(
                    directory_name,
                    0o700,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                ),
            )
            _checked_call(
                deadline,
                "protocol witness run root fsync",
                lambda: os.fsync(root_fd),
            )
            directory_fd = _checked_open(
                deadline,
                "protocol witness fixture directory open",
                lambda: os.open(
                    directory_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                ),
            )
            directory_stat = _checked_call(
                deadline,
                "protocol witness fixture directory fstat",
                lambda: os.fstat(directory_fd),
            )
            file_fd = _checked_open(
                deadline,
                "protocol witness fixture file open",
                lambda: os.open(
                    _FIXTURE_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                ),
            )
            try:
                _checked_call(
                    deadline,
                    "protocol witness fixture file chmod",
                    lambda: os.fchmod(file_fd, 0o600),
                )
                view = memoryview(FIXTURE_BYTES)
                while view:
                    current_view = view
                    written = _checked_call(
                        deadline,
                        "protocol witness fixture write",
                        lambda current_view=current_view: os.write(file_fd, current_view),
                    )
                    if written <= 0:
                        raise OSError("fixture write made no progress")
                    view = view[written:]
                _checked_call(
                    deadline,
                    "protocol witness fixture file fsync",
                    lambda: os.fsync(file_fd),
                )
            finally:
                os.close(file_fd)
            _checked_call(
                deadline,
                "protocol witness fixture directory fsync",
                lambda: os.fsync(directory_fd),
            )
            return cls(
                owned_root=owned_root,
                directory_name=directory_name,
                directory_path=(
                    owned_root / directory_name
                    if owned_root_fd is None
                    else Path(f"/proc/{os.getpid()}/fd/{root_fd}") / directory_name
                ),
                root_fd=root_fd,
                directory_fd=directory_fd,
                directory_identity=(directory_stat.st_dev, directory_stat.st_ino),
            )
        except BaseException as error:
            if directory_fd >= 0:
                if created:
                    try:
                        _checked_call(
                            deadline,
                            "protocol witness setup rollback unlink",
                            lambda: os.unlink(_FIXTURE_NAME, dir_fd=directory_fd),
                        )
                        _checked_call(
                            deadline,
                            "protocol witness setup rollback directory fsync",
                            lambda: os.fsync(directory_fd),
                        )
                    except (OSError, DeadlineExceeded) as cleanup_error:
                        if isinstance(error, Exception):
                            error.add_note(
                                "setup rollback could not remove the fixture file: "
                                f"{type(cleanup_error).__name__}"
                            )
                os.close(directory_fd)
            if created:
                try:
                    _checked_call(
                        deadline,
                        "protocol witness setup rollback rmdir",
                        lambda: os.rmdir(directory_name, dir_fd=root_fd),
                    )
                    _checked_call(
                        deadline,
                        "protocol witness setup rollback root fsync",
                        lambda: os.fsync(root_fd),
                    )
                except (OSError, DeadlineExceeded) as cleanup_error:
                    if isinstance(error, Exception):
                        error.add_note(
                            "setup rollback could not remove the fixture directory: "
                            f"{type(cleanup_error).__name__}"
                        )
            os.close(root_fd)
            if isinstance(error, DeadlineExceeded):
                raise
            if isinstance(error, ProtocolWitnessSetupError):
                raise
            if isinstance(error, Exception):
                raise ProtocolWitnessSetupError(
                    f"could not create disposable fixture: {type(error).__name__}"
                ) from error
            raise

    def verify(self, *, deadline: Deadline) -> tuple[bool, int, str | None]:
        try:
            current_directory = _checked_call(
                deadline,
                "protocol witness verify directory stat",
                lambda: os.stat(
                    self.directory_name,
                    dir_fd=self.root_fd,
                    follow_symlinks=False,
                ),
            )
            if (current_directory.st_dev, current_directory.st_ino) != self.directory_identity:
                return False, 0, "disposable fixture directory changed identity"
            file_fd = _checked_open(
                deadline,
                "protocol witness verify file open",
                lambda: os.open(
                    _FIXTURE_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=self.directory_fd,
                ),
            )
            try:
                observed = _checked_call(
                    deadline,
                    "protocol witness verify file fstat",
                    lambda: os.fstat(file_fd),
                )
                if not stat.S_ISREG(observed.st_mode):
                    return False, stat.S_IMODE(observed.st_mode), "fixture changed file type"
                chunks: list[bytes] = []
                while True:
                    chunk = _checked_call(
                        deadline,
                        "protocol witness verify read",
                        lambda: os.read(file_fd, 64 * 1024),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                payload = b"".join(chunks)
                mode = stat.S_IMODE(observed.st_mode)
            finally:
                os.close(file_fd)
        except OSError as error:
            return False, 0, f"fixture changed or became unreadable: {type(error).__name__}"
        if payload != FIXTURE_BYTES:
            return False, mode, "fixture changed bytes during witness session"
        if mode != 0o600:
            return False, mode, f"fixture changed mode to {oct(mode)}"
        return True, mode, None

    def cleanup(self, *, deadline: Deadline) -> str | None:
        issue: str | None = None
        logical_directory_matches = False
        try:
            logical_directory = _checked_call(
                deadline,
                "protocol witness cleanup directory stat",
                lambda: os.stat(
                    self.directory_name,
                    dir_fd=self.root_fd,
                    follow_symlinks=False,
                ),
            )
            logical_directory_matches = (
                logical_directory.st_dev,
                logical_directory.st_ino,
            ) == self.directory_identity
        except OSError:
            pass
        except DeadlineExceeded:
            os.close(self.directory_fd)
            os.close(self.root_fd)
            raise
        try:
            _checked_call(
                deadline,
                "protocol witness cleanup unlink",
                lambda: os.unlink(_FIXTURE_NAME, dir_fd=self.directory_fd),
            )
            _checked_call(
                deadline,
                "protocol witness cleanup directory fsync",
                lambda: os.fsync(self.directory_fd),
            )
        except DeadlineExceeded:
            os.close(self.directory_fd)
            os.close(self.root_fd)
            raise
        except OSError as error:
            issue = f"disposable fixture cleanup failed: {type(error).__name__}"
        os.close(self.directory_fd)
        if issue is None and not logical_directory_matches:
            issue = "disposable fixture directory changed identity before cleanup"
        if issue is None:
            try:
                _checked_call(
                    deadline,
                    "protocol witness cleanup rmdir",
                    lambda: os.rmdir(self.directory_name, dir_fd=self.root_fd),
                )
                _checked_call(
                    deadline,
                    "protocol witness cleanup root fsync",
                    lambda: os.fsync(self.root_fd),
                )
            except DeadlineExceeded:
                os.close(self.root_fd)
                raise
            except OSError as error:
                issue = f"disposable fixture directory cleanup failed: {type(error).__name__}"
        os.close(self.root_fd)
        return issue


def run_protocol_behavior_witness(
    request: ProtocolWitnessRequest,
    *,
    deadline: Deadline,
    probe_runner: _ProbeRunner = run_protocol_probe,
) -> ProtocolBehaviorWitness:
    """Run one candidate session and return bounded behavior evidence, never raw source."""

    fixture = _DisposableFixture.create(
        request.owned_root,
        request.candidate,
        deadline=deadline,
        owned_root_fd=request.owned_root_fd,
    )
    fixture_uri = (fixture.directory_path / _FIXTURE_NAME).as_uri()
    diagnostics = _DiagnosticsObservation(fixture_uri)
    configuration = _ConfigurationObservation()
    issues: list[str] = []
    session_result: _SessionResult | None = None
    protocol_session: ProtocolSession[Any] | None = None
    started_elapsed = deadline.elapsed()
    wrapped_spec = _observed_spec(request.spec, configuration, diagnostics)
    selected_interpreter = str(_ms_interpreter(request.runtime))

    def session(client: SyncLspClient, providers: RawLspProviders) -> _SessionResult:
        nonlocal session_result
        _send_configuration(
            request.candidate,
            request.runtime,
            client,
            configuration,
        )
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": fixture_uri,
                    "languageId": "python",
                    "version": 1,
                    "text": FIXTURE_BYTES.decode("utf-8"),
                }
            },
        )
        diagnostics.arm(document_version=1)
        try:
            external: Location | None = None
            local_y: Location | None = None
            first_count = 0
            first_ready: float | None = None
            if providers.definition:
                external_locations = _request_locations(
                    client,
                    deadline,
                    fixture_uri,
                    line=1,
                    character=25,
                )
                external = _external_transformers_location(
                    external_locations,
                    deadline=deadline,
                )
                if external is not None:
                    first_count = len(external_locations)
                    first_ready = max(0.0, deadline.elapsed() - started_elapsed)
                local_locations = _request_locations(
                    client,
                    deadline,
                    fixture_uri,
                    line=6,
                    character=11,
                )
                local_y = next(
                    (location for location in local_locations if location.uri == fixture_uri),
                    None,
                )
            if request.spec.diagnostics_mode == "push":
                deadline.check("protocol witness diagnostics wait before")
                remaining = deadline.remaining()
                if remaining > 0:
                    assert diagnostics.event is not None
                    diagnostics.event.wait(min(_PUSH_DIAGNOSTICS_WAIT_SECONDS, remaining))
                deadline.check("protocol witness diagnostics wait after")
            session_result = _SessionResult(
                external_definition=external,
                local_y_definition=local_y,
                first_normalized_count=first_count,
                first_readiness_seconds=first_ready,
            )
            return session_result
        finally:
            client.notify("textDocument/didClose", {"textDocument": {"uri": fixture_uri}})

    transport = "unobserved"
    config_interpreter: str | None = None
    config_path: str | None = None
    payload_sha: str | None = None
    config_application_proven = False
    external_relative: str | None = None
    position_encoding = request.spec.position_encoding
    y_raw: tuple[int, int, int, int] | None = None
    y_decoded: tuple[int, int, int, int] | None = None
    push_claimed = request.spec.diagnostics_mode == "push"
    diagnostics_completion_reason = "not_applicable_pull"
    unchanged = False
    mode = 0
    phase_deadline_error: DeadlineExceeded | None = None
    try:
        try:
            protocol_session = cast(
                "ProtocolSession[Any]",
                probe_runner(
                    wrapped_spec,
                    request.runtime,
                    fixture.directory_path,
                    deadline=deadline,
                    session=cast("Callable[[SyncLspClient, RawLspProviders], object]", session),
                ),
            )
            session_result = cast("_SessionResult", protocol_session.result)
        except DeadlineExceeded:
            raise
        except Exception as error:
            protocol_session = protocol_session_from_error(error)
            issues.append(
                f"protocol witness session failed: {type(error).__name__}: "
                f"{redacted_evidence_text(error)}"
            )

        engine_interpreter = (
            None
            if protocol_session is None or protocol_session.engine.interpreter is None
            else str(protocol_session.engine.interpreter)
        )
        if protocol_session is not None:
            if protocol_session.terminal_errors:
                issues.append(
                    "protocol witness recorded "
                    f"{len(protocol_session.terminal_errors)} terminal error(s)"
                )
            if protocol_session.cleanup_errors:
                issues.append(
                    "protocol witness recorded "
                    f"{len(protocol_session.cleanup_errors)} cleanup error(s)"
                )
            if protocol_session.exit_status not in (None, 0):
                issues.append(
                    f"candidate exited with status {protocol_session.exit_status}"
                )
        if engine_interpreter != selected_interpreter:
            issues.append("candidate engine did not bind the exact frozen ms interpreter")

        external_relative = _external_relative_path(
            session_result,
            issues,
            deadline=deadline,
        )
        (
            transport,
            config_interpreter,
            config_path,
            payload_sha,
            config_application_proven,
        ) = _configuration_evidence(
            request,
            configuration,
            external_definition_proven=external_relative is not None,
            issues=issues,
        )
        position_encoding = (
            protocol_session.position_encoding
            if protocol_session is not None
            else request.spec.position_encoding
        )
        y_raw, y_decoded = _position_evidence(
            session_result,
            fixture_uri,
            position_encoding,
            issues,
        )
        if push_claimed and not diagnostics.exact_uri_observed:
            issues.append("push diagnostics claimed but none were published for the exact fixture URI")
        if push_claimed and not diagnostics.missing_import_observed:
            issues.append("push diagnostics did not report the fixture missing import")
        if push_claimed:
            diagnostics_completion_reason = (
                "missing_import_observed"
                if diagnostics.missing_import_observed
                else (
                    "bounded_wait_without_required_diagnostic"
                    if diagnostics.exact_uri_observed
                    else "bounded_wait_without_publication"
                )
            )
        if session_result is None or session_result.first_normalized_count < 1:
            issues.append("definition produced no first normalized readiness evidence")

        unchanged, mode, fixture_issue = fixture.verify(deadline=deadline)
        if fixture_issue is not None:
            issues.append(fixture_issue)
    except DeadlineExceeded as error:
        phase_deadline_error = error
        raise
    finally:
        try:
            cleanup_issue = fixture.cleanup(deadline=deadline)
        except DeadlineExceeded as cleanup_error:
            if phase_deadline_error is None:
                raise
            phase_deadline_error.add_note(
                "protocol witness cleanup also reached the same phase deadline: "
                f"{cleanup_error}"
            )
        else:
            if cleanup_issue is not None:
                issues.append(cleanup_issue)

    bounded_issues = _bounded_issues(issues)
    providers = (
        protocol_session.raw_providers
        if protocol_session is not None
        else RawLspProviders()
    )
    raw_providers = tuple(
        sorted(
            {
                "declaration": providers.declaration,
                "definition": providers.definition,
                "document_symbols": providers.document_symbols,
                "implementation": providers.implementation,
                "references": providers.references,
                "workspace_symbols": providers.workspace_symbols,
            }.items()
        )
    )
    return ProtocolBehaviorWitness(
        schema_version=PROTOCOL_WITNESS_SCHEMA_VERSION,
        candidate=request.candidate,
        passed=not bounded_issues,
        fixture_sha256=sha256_bytes(FIXTURE_BYTES),
        fixture_mode=mode,
        fixture_unchanged=unchanged,
        selected_interpreter=selected_interpreter,
        configuration_transport=transport,
        configuration_interpreter=config_interpreter,
        configuration_path=config_path,
        configuration_payload_sha256=payload_sha,
        configuration_request_count=len(configuration.request_payloads or ()),
        configuration_application_proven=config_application_proven,
        external_definition_relative_path=external_relative,
        position_encoding=position_encoding.value,
        y_raw_range=y_raw,
        y_decoded_range=y_decoded,
        push_diagnostics_claimed=push_claimed,
        exact_uri_diagnostics=diagnostics.exact_uri_observed,
        missing_import_diagnostic=diagnostics.missing_import_observed,
        exact_uri_publish_count=diagnostics.exact_uri_count,
        exact_uri_diagnostic_count=diagnostics.diagnostic_count,
        diagnostics_completion_reason=diagnostics_completion_reason,
        first_normalized_capability=(
            "definition"
            if session_result is not None and session_result.first_normalized_count > 0
            else None
        ),
        first_normalized_count=(
            0 if session_result is None else session_result.first_normalized_count
        ),
        first_readiness_seconds=(
            None if session_result is None else session_result.first_readiness_seconds
        ),
        raw_providers=raw_providers,
        terminal_error_count=(
            0 if protocol_session is None else len(protocol_session.terminal_errors)
        ),
        cleanup_error_count=(
            0 if protocol_session is None else len(protocol_session.cleanup_errors)
        ),
        issues=bounded_issues,
    )


def _observed_spec(
    spec: BackendProtocolSpec,
    configuration: _ConfigurationObservation,
    diagnostics: _DiagnosticsObservation,
) -> BackendProtocolSpec:
    def initialize_params(root: Path) -> Mapping[str, object]:
        params = dict(spec.initialize_params(root))
        configuration.initialize_params = params
        return params

    handlers: dict[str, Callable[[Any], Any]] | None = None
    if spec.request_handlers is not None:
        handlers = dict(spec.request_handlers)
        original_configuration = handlers.get("workspace/configuration")
        if original_configuration is not None:
            def observe_configuration(params: Any) -> Any:
                result = original_configuration(params)
                assert configuration.request_payloads is not None
                configuration.request_payloads.append((params, result))
                return result

            handlers["workspace/configuration"] = observe_configuration

    def observe_notification(method: str, params: Any) -> None:
        diagnostics.observe(method, params)
        if spec.notification_handler is not None:
            spec.notification_handler(method, params)

    return replace(
        spec,
        initialize_params=initialize_params,
        request_handlers=handlers,
        notification_handler=observe_notification,
    )


def _send_configuration(
    candidate: str,
    runtime: CandidateRuntime,
    client: SyncLspClient,
    observation: _ConfigurationObservation,
) -> None:
    if candidate == "pyright":
        payload: object = {"settings": {}}
    elif candidate == "ty":
        return
    else:
        return
    observation.sent_change_configuration = payload
    client.notify("workspace/didChangeConfiguration", payload)


def _configuration_evidence(
    request: ProtocolWitnessRequest,
    observation: _ConfigurationObservation,
    *,
    external_definition_proven: bool,
    issues: list[str],
) -> tuple[str, str | None, str | None, str | None, bool]:
    expected_interpreter = str(_ms_interpreter(request.runtime))
    if request.candidate == "pyright":
        requests = observation.request_payloads or []
        response = [response for _params, response in requests]
        observed_interpreter = _find_nested_string(response, "pythonPath")
        if observed_interpreter != expected_interpreter:
            issues.append("Pyright workspace/configuration did not return the frozen ms interpreter")
        if not requests:
            issues.append("Pyright issued no observed workspace/configuration request")
        application_proven = bool(requests) and external_definition_proven
        if not application_proven:
            issues.append(
                "Pyright configuration application was not proven by a server request and "
                "an external definition under the frozen ms environment"
            )
        return (
            "workspace_configuration",
            observed_interpreter,
            None,
            _payload_digest({"requests": requests}, issues),
            application_proven,
        )
    config = _service_config(request.runtime, request.candidate)
    if request.candidate == "ty":
        requests = observation.request_payloads or []
        expected_scope_uri = (observation.initialize_params or {}).get("rootUri")
        expected_request = {
            "items": [{"scopeUri": expected_scope_uri, "section": "ty"}]
        }
        expected_response = [
            {
                "configurationFile": config.config_path,
                "configuration": {
                    "environment": {"python": expected_interpreter},
                },
            }
        ]
        responses = [response for _params, response in requests]
        observed_interpreter = _find_nested_string(responses, "python")
        observed_config = _find_nested_string(responses, "configurationFile")
        exact_request_observed = requests == [(expected_request, expected_response)]
        if observed_interpreter != expected_interpreter or observed_config != config.config_path:
            issues.append(
                "ty workspace/configuration did not return the exact interpreter and config"
            )
        if not exact_request_observed:
            issues.append(
                "ty issued no exact scoped workspace/configuration server request"
            )
        application_proven = (
            observed_interpreter == expected_interpreter
            and observed_config == config.config_path
            and exact_request_observed
            and external_definition_proven
        )
        if not application_proven:
            issues.append(
                "ty configuration application was not proven by an exact server request and "
                "an external definition under the frozen ms environment"
            )
        return (
            "workspace_configuration",
            observed_interpreter,
            observed_config,
            _payload_digest({"requests": requests}, issues),
            application_proven,
        )
    initialize_params = observation.initialize_params or {}
    options = initialize_params.get("initializationOptions")
    observed_interpreter = _find_nested_string(options, "pythonPath")
    observed_config = _find_nested_string(options, "configPath")
    if observed_interpreter != expected_interpreter or observed_config != config.config_path:
        issues.append("Pyrefly initializationOptions did not bind the exact interpreter and config")
    application_proven = (
        observed_interpreter == expected_interpreter
        and observed_config == config.config_path
        and external_definition_proven
    )
    if not application_proven:
        issues.append(
            "Pyrefly configuration application was not proven by an external definition under "
            "the frozen ms environment"
        )
    return (
        "initialization_options",
        observed_interpreter,
        observed_config,
        _payload_digest({"initializationOptions": options}, issues),
        application_proven,
    )


def _request_locations(
    client: SyncLspClient,
    deadline: Deadline,
    uri: str,
    *,
    line: int,
    character: int,
) -> tuple[Location, ...]:
    deadline.check("protocol behavior witness definition")
    raw = client.request(
        "textDocument/definition",
        {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        },
        timeout=deadline.remaining(),
    )
    if raw is None:
        entries: Sequence[object] = ()
    elif isinstance(raw, Mapping):
        entries = (raw,)
    elif isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        entries = raw
    else:
        raise NormalizationError("definition result must be a location, sequence, or null")
    locations: list[Location] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise NormalizationError("definition result contains a non-object location")
        locations.append(normalize_location(cast("Mapping[str, Any]", entry)))
    return tuple(locations)


def _external_transformers_location(
    locations: Sequence[Location],
    *,
    deadline: Deadline,
) -> Location | None:
    frozen_root = _checked_call(
        deadline,
        "protocol witness frozen transformers root resolve",
        lambda: MS_TRANSFORMERS_ROOT.resolve(strict=True),
    )
    for location in locations:
        deadline.check("protocol witness external definition loop")
        location_path = location.path
        if location_path is None:
            continue
        unresolved = Path(location_path)
        try:
            resolved = _checked_call(
                deadline,
                "protocol witness external definition resolve",
                lambda unresolved=unresolved: unresolved.resolve(strict=True),
            )
        except OSError:
            continue
        if resolved.is_relative_to(frozen_root):
            return location
    return None


def _external_relative_path(
    result: _SessionResult | None,
    issues: list[str],
    *,
    deadline: Deadline,
) -> str | None:
    if result is None:
        issues.append("GenerationConfig definition did not resolve under the frozen ms transformers root")
        return None
    definition = result.external_definition
    if definition is None or definition.path is None:
        issues.append("GenerationConfig definition did not resolve under the frozen ms transformers root")
        return None
    unresolved = Path(definition.path)
    path = _checked_call(
        deadline,
        "protocol witness selected external definition resolve",
        lambda: unresolved.resolve(strict=True),
    )
    frozen_root = _checked_call(
        deadline,
        "protocol witness selected transformers root resolve",
        lambda: MS_TRANSFORMERS_ROOT.resolve(strict=True),
    )
    return path.relative_to(frozen_root).as_posix()


def _position_evidence(
    result: _SessionResult | None,
    fixture_uri: str,
    encoding: PositionEncoding,
    issues: list[str],
) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    if result is None or result.local_y_definition is None:
        issues.append("local y definition returned no exact-fixture location")
        return None, None
    location = result.local_y_definition
    if location.uri != fixture_uri:
        issues.append("local y definition returned a different URI")
        return None, None
    raw = (
        location.range.start.line,
        location.range.start.character,
        location.range.end.line,
        location.range.end.character,
    )
    renderer = PublicPositionRenderer.from_snapshot(
        FileSnapshot.from_bytes(FIXTURE_BYTES),
        encoding,
    )
    start = LspPosition(location.range.start.line, location.range.start.character)
    end = LspPosition(location.range.end.line, location.range.end.character)
    try:
        rendered = renderer.range(start, end)
        selected_text = renderer.text(start, end)
    except PositionError as error:
        issues.append(f"negotiated position mapping rejected the server y range: {error}")
        return raw, None
    if selected_text != "y":
        issues.append("negotiated position mapping did not select the fixture y identifier")
        return raw, None
    decoded = (
        rendered["start"]["line"],
        rendered["start"]["column"],
        rendered["end"]["line"],
        rendered["end"]["column"],
    )
    return raw, decoded


def _ms_interpreter(runtime: CandidateRuntime) -> Path:
    matches = tuple(
        Path(identity.interpreter_path)
        for identity in runtime.environments
        if identity.name == "ms"
    )
    if len(matches) != 1:
        raise ValueError("candidate runtime must bind exactly one frozen ms interpreter")
    return matches[0]


def _service_config(runtime: CandidateRuntime, candidate: str) -> Any:
    matches = tuple(
        identity for identity in runtime.service_configs if identity.backend == candidate
    )
    if len(matches) != 1:
        raise ValueError(f"candidate runtime must bind exactly one {candidate} service config")
    return matches[0]


def _find_nested_string(value: object, key: str) -> str | None:
    if isinstance(value, Mapping):
        typed_value = cast("Mapping[object, object]", value)
        candidate = typed_value.get(key)
        if isinstance(candidate, str):
            return candidate
        for child in typed_value.values():
            found = _find_nested_string(child, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for child in value:
            found = _find_nested_string(child, key)
            if found is not None:
                return found
    return None


def _payload_digest(value: Mapping[str, object], issues: list[str]) -> str | None:
    try:
        return sha256_bytes(canonical_json(value))
    except (TypeError, ValueError) as error:
        issues.append(f"configuration transport was not canonical JSON: {type(error).__name__}")
        return None


def _bounded_issues(issues: Sequence[str]) -> tuple[str, ...]:
    rendered = sorted(
        {
            redacted_evidence_text(issue)[:_MAX_ISSUE_CHARS]
            for issue in issues
            if issue
        }
    )
    if len(rendered) <= _MAX_ISSUES:
        return tuple(rendered)
    omitted = len(rendered) - (_MAX_ISSUES - 1)
    return (*rendered[: _MAX_ISSUES - 1], f"{omitted} additional issues omitted")


def _checked_call[T](deadline: Deadline, step: str, operation: Callable[[], T]) -> T:
    """Bracket one non-open filesystem operation with the caller's same ceiling."""

    deadline.check(f"{step} before")
    result = operation()
    deadline.check(f"{step} after")
    return result


def _open_owned_run_root(owned_root: Path, *, deadline: Deadline) -> int:
    """Guard the one absolute run-root open; descendants remain descriptor-confined."""

    descriptor = _checked_open(
        deadline,
        "protocol witness run root open",
        lambda: os.open(
            owned_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        ),
    )
    try:
        observed = _checked_call(
            deadline,
            "protocol witness run root fstat",
            lambda: os.fstat(descriptor),
        )
        if not stat.S_ISDIR(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o700:
            raise ProtocolWitnessSetupError(
                "caller-provided per-run root must be a 0700 directory"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _duplicate_owned_run_root(owned_root_fd: int, *, deadline: Deadline) -> int:
    """Duplicate an orchestrator-retained run root without trusting its logical path."""

    descriptor = _checked_open(
        deadline,
        "protocol witness run root duplicate",
        lambda: os.dup(owned_root_fd),
    )
    try:
        source = _checked_call(
            deadline,
            "protocol witness source run root fstat",
            lambda: os.fstat(owned_root_fd),
        )
        observed = _checked_call(
            deadline,
            "protocol witness duplicated run root fstat",
            lambda: os.fstat(descriptor),
        )
        if (
            (source.st_dev, source.st_ino) != (observed.st_dev, observed.st_ino)
            or not stat.S_ISDIR(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise ProtocolWitnessSetupError(
                "retained per-run descriptor must name its exact 0700 directory"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _checked_open(deadline: Deadline, step: str, operation: Callable[[], int]) -> int:
    """Bracket an open and close its new descriptor if the post-check expires."""

    deadline.check(f"{step} before")
    descriptor = operation()
    try:
        deadline.check(f"{step} after")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
