"""Owned language-server process launch and cleanup primitives.

The Linux parent-death launcher is derived from Serena/SolidLSP at the pinned
commit recorded in ``third_party/copied_sources.json``.  Its dedicated spawner
thread is intentionally separate from any executor used for ordinary LSP work:
Linux associates ``PR_SET_PDEATHSIG`` with the thread that calls ``fork``.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import platform
import queue
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import ClassVar

import psutil

log = logging.getLogger(__name__)

type Command = str | Sequence[str]
type SpawnCallable = Callable[[], subprocess.Popen[bytes]]
type SpawnResult = tuple[BaseException | None, subprocess.Popen[bytes] | None]


class LanguageServerSubprocessLauncher:
    """Launch protected language servers from one persistent spawner thread."""

    _PR_SET_PDEATHSIG = 1

    _instance: ClassVar[LanguageServerSubprocessLauncher | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()
    _spawner: ClassVar[_PDeathSigSpawner | None] = None
    _spawner_pid: ClassVar[int | None] = None
    _spawner_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._libc = self._load_libc()

    @classmethod
    def get_instance(cls) -> LanguageServerSubprocessLauncher:
        """Return the process-wide launcher instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @staticmethod
    def _load_libc() -> ctypes.CDLL | None:
        if platform.system() != "Linux":
            return None
        try:
            return ctypes.CDLL(None, use_errno=True)
        except OSError as exc:
            log.warning(
                "Could not load libc (%s); child processes lack Linux parent-death protection",
                exc,
            )
            return None

    def _set_pdeathsig_on_parent_exit(self) -> None:
        """Register SIGTERM for the protected child when its spawning thread dies."""
        if self._libc is None:
            return
        result = self._libc.prctl(self._PR_SET_PDEATHSIG, signal.SIGTERM)
        if result != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))

    @classmethod
    def _get_spawner(cls) -> _PDeathSigSpawner:
        current_pid = os.getpid()
        with cls._spawner_lock:
            if cls._spawner is None or cls._spawner_pid != current_pid or not cls._spawner.is_alive():
                cls._spawner = cls._PDeathSigSpawner()
                cls._spawner_pid = current_pid
            return cls._spawner

    def launch(
        self,
        command: Command,
        *,
        cwd: str | Path,
        env: Mapping[str, str | None] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Launch a language server in an owned session with Linux PDEATHSIG.

        ``Popen`` always executes on the persistent spawner thread when Linux
        parent-death protection is available.  ``exec`` replaces the shell so
        the registration and PID belong to the actual language-server process.
        """
        child_env = os.environ.copy()
        if env is not None:
            for name, value in env.items():
                if value is None:
                    child_env.pop(name, None)
                else:
                    child_env[name] = value

        shell_command = command if isinstance(command, str) else shlex.join(command)
        use_pdeathsig = self._libc is not None
        if use_pdeathsig:
            shell_command = f"exec {shell_command}"

        def do_popen() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                shell_command,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                start_new_session=True,
                preexec_fn=self._set_pdeathsig_on_parent_exit if use_pdeathsig else None,
            )

        if not use_pdeathsig:
            return do_popen()
        return self._get_spawner().spawn(do_popen)

    class _PDeathSigSpawner:
        """Run protected ``Popen`` calls on one daemon-lifetime thread."""

        def __init__(self) -> None:
            self._queue: queue.Queue[tuple[SpawnCallable, queue.Queue[SpawnResult]]] = queue.Queue()
            self._thread = threading.Thread(
                target=self._run,
                name="serena-light-pdeathsig-spawner",
                daemon=True,
            )
            self._thread.start()

        def is_alive(self) -> bool:
            return self._thread.is_alive()

        def _run(self) -> None:
            while True:
                function, result_queue = self._queue.get()
                try:
                    result_queue.put((None, function()))
                except BaseException as exc:
                    result_queue.put((exc, None))

        def spawn(self, function: SpawnCallable) -> subprocess.Popen[bytes]:
            result_queue: queue.Queue[SpawnResult] = queue.Queue(maxsize=1)
            self._queue.put((function, result_queue))
            error, process = result_queue.get()
            if error is not None:
                raise error
            if process is None:  # pragma: no cover - internal queue invariant
                raise RuntimeError("spawner returned neither a process nor an error")
            return process


def _owned_process_group(process: subprocess.Popen[bytes]) -> int | None:
    """Return the child's process group only when it is safe for us to signal."""
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return None
    if process_group != process.pid or process_group == os.getpgrp():
        return None
    return process_group


def _signal_process_tree(
    process: subprocess.Popen[bytes],
    terminate: bool = True,
    *,
    process_group: int | None = None,
) -> int | None:
    """Signal an owned process group, falling back to the captured process tree."""
    signal_number = signal.SIGTERM if terminate else signal.SIGKILL
    owned_group = process_group if process_group is not None else _owned_process_group(process)
    if owned_group is not None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(owned_group, signal_number)
        return owned_group

    try:
        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        descendants = []
    for target in [*descendants, process]:
        with contextlib.suppress(OSError, psutil.Error):
            target.terminate() if terminate else target.kill()
    return None


def _process_group_has_live_members(process_group: int) -> bool:
    for candidate in psutil.process_iter(["pid", "status"]):
        try:
            if candidate.info["status"] == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(candidate.pid) == process_group:
                return True
        except (OSError, psutil.Error):
            continue
    return False


def _wait_for_exit(process: subprocess.Popen[bytes], process_group: int | None, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_group is not None:
            if not _process_group_has_live_members(process_group):
                return True
        elif process.poll() is not None:
            return True
        time.sleep(0.02)
    if process_group is not None:
        return not _process_group_has_live_members(process_group)
    return process.poll() is not None


def terminate_process_tree_with_kill_fallback(
    process: subprocess.Popen[bytes],
    terminate_timeout: float,
    process_name: str = "Process",
    *,
    kill_timeout: float = 2.0,
) -> None:
    """Terminate an owned process group, then kill it if the deadline expires."""
    if terminate_timeout < 0 or kill_timeout < 0:
        raise ValueError("process cleanup timeouts must be non-negative")

    process_group = _owned_process_group(process)
    log.debug("Terminating %s pid=%s status=%s", process_name, process.pid, process.poll())
    process_group = _signal_process_tree(process, terminate=True, process_group=process_group)
    if not _wait_for_exit(process, process_group, terminate_timeout):
        log.warning("%s pid=%s did not terminate; killing its owned process tree", process_name, process.pid)
        _signal_process_tree(process, terminate=False, process_group=process_group)
        if not _wait_for_exit(process, process_group, kill_timeout):
            raise TimeoutError(f"{process_name} pid={process.pid} survived SIGKILL cleanup")

    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.1)
