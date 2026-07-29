"""Linux regressions for thread-scoped parent-death protection."""

from __future__ import annotations

import os
import platform
import selectors
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from serena_light.bootstrap import repository_root, runtime_paths
from serena_light.processes import terminate_process_tree_with_kill_fallback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.admission.lsp_probe import LspClient, Profile, initialize_params, path_uri  # noqa: E402

pytestmark = pytest.mark.skipif(platform.system() != "Linux", reason="PR_SET_PDEATHSIG is Linux-specific")

SRC = ROOT / "src"
_SERVER_COMMAND = "import time; time.sleep(300)"

_THREAD_DRIVER = textwrap.dedent(
    f"""
    import sys
    import threading
    import time

    sys.path.insert(0, sys.argv[1])
    from serena_light.processes import LanguageServerSubprocessLauncher

    holder = {{}}
    def start_server():
        holder["process"] = LanguageServerSubprocessLauncher.get_instance().launch(
            [sys.executable, "-c", {_SERVER_COMMAND!r}], cwd="/tmp"
        )

    caller = threading.Thread(target=start_server)
    caller.start()
    caller.join()
    print(f"READY {{holder['process'].pid}}", flush=True)
    time.sleep(600)
    """
)

_UPSTREAM_SIGKILL_DRIVER = textwrap.dedent(
    f"""
    import sys
    import time

    sys.path.insert(0, sys.argv[1])
    from serena_light.processes import LanguageServerSubprocessLauncher

    server = LanguageServerSubprocessLauncher.get_instance().launch(
        [sys.executable, "-c", {_SERVER_COMMAND!r}], cwd="/tmp"
    )
    print(f"READY {{server.pid}}", flush=True)
    time.sleep(600)
    """
)

_DAEMON_PATH_DRIVER = textwrap.dedent(
    f"""
    import sys
    import time

    sys.path.insert(0, sys.argv[1])
    from serena_light.processes import LanguageServerSubprocessLauncher

    def start_adapter():
        return LanguageServerSubprocessLauncher.get_instance().launch(
            [sys.executable, "-c", {_SERVER_COMMAND!r}], cwd="/tmp"
        )

    server = start_adapter()
    print(f"READY {{server.pid}}", flush=True)
    time.sleep(600)
    """
)


def _start_driver(source: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", source, str(SRC)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _read_server_identity(driver: subprocess.Popen[bytes], timeout: float = 8.0) -> tuple[int, float]:
    assert driver.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(driver.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), f"driver pid={driver.pid} did not become ready"
        line = driver.stdout.readline().decode().strip()
    finally:
        selector.close()
    assert line.startswith("READY "), f"unexpected driver readiness line: {line!r}"
    server_pid = int(line.removeprefix("READY "))
    return server_pid, psutil.Process(server_pid).create_time()


def _same_live_process(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == create_time and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def _wait_until_gone(pid: int, create_time: float, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while _same_live_process(pid, create_time) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _same_live_process(pid, create_time), f"owned server pid={pid} survived its daemon"


def _cleanup(driver: subprocess.Popen[bytes], server_identity: tuple[int, float] | None) -> None:
    if driver.poll() is None:
        driver.kill()
    try:
        driver.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        if os.getpgid(driver.pid) == driver.pid:
            os.killpg(driver.pid, signal.SIGKILL)
        driver.wait(timeout=2.0)

    if server_identity is not None:
        server_pid, create_time = server_identity
        if _same_live_process(server_pid, create_time):
            try:
                if os.getpgid(server_pid) == server_pid:
                    os.killpg(server_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for stream in (driver.stdout, driver.stderr):
        if stream is not None:
            stream.close()


def _kill_group_if_identity_is_still_a_member(process_group: int, identities: list[tuple[int, float]]) -> None:
    for pid, create_time in identities:
        if not _same_live_process(pid, create_time):
            continue
        try:
            if os.getpgid(pid) == process_group:
                os.killpg(process_group, signal.SIGKILL)
                return
        except ProcessLookupError:
            continue


def test_server_survives_short_lived_calling_thread() -> None:
    driver = _start_driver(_THREAD_DRIVER)
    identity: tuple[int, float] | None = None
    try:
        identity = _read_server_identity(driver)
        time.sleep(0.5)
        assert driver.poll() is None
        assert _same_live_process(*identity), "the calling thread's return killed the protected server"
    finally:
        _cleanup(driver, identity)


def test_server_dies_with_sigkilled_parent() -> None:
    driver = _start_driver(_UPSTREAM_SIGKILL_DRIVER)
    identity: tuple[int, float] | None = None
    try:
        identity = _read_server_identity(driver)
        assert _same_live_process(*identity)

        driver.kill()
        driver.wait(timeout=2.0)

        _wait_until_gone(*identity)
    finally:
        _cleanup(driver, identity)


def test_real_daemon_launch_path_survives_return_then_leaves_no_descendant() -> None:
    driver = _start_driver(_DAEMON_PATH_DRIVER)
    identity: tuple[int, float] | None = None
    try:
        identity = _read_server_identity(driver)
        time.sleep(0.5)
        assert driver.poll() is None
        assert _same_live_process(*identity)

        driver.kill()
        driver.wait(timeout=2.0)

        _wait_until_gone(*identity)
    finally:
        _cleanup(driver, identity)


@pytest.mark.parametrize("engine", ["pyright", "typescript"])
def test_normal_cleanup_exits_real_language_server_tree(tmp_path: Path, engine: str) -> None:
    locked = runtime_paths(repository_root())
    settings: dict[str, Any] = {}
    if engine == "pyright":
        source = tmp_path / "main.py"
        source.write_text("answer: int = 42\n", encoding="utf-8")
        profile = Profile(
            "process-cleanup",
            "python",
            tmp_path,
            "answer",
            Path("main.py"),
            Path("/root/miniconda3/envs/ms/bin/python"),
        )
        command = [str(locked["node"]), str(locked["pyright-langserver"]), "--stdio"]
        settings = {"pythonPath": str(profile.interpreter), "analysis": {"diagnosticMode": "openFilesOnly"}}
    else:
        source = tmp_path / "main.ts"
        source.write_text("export const answer: number = 42;\n", encoding="utf-8")
        profile = Profile(
            "process-cleanup",
            "typescript",
            tmp_path,
            "answer",
            Path("main.ts"),
            representative=Path("main.ts"),
        )
        command = [str(locked["node"]), str(locked["typescript-language-server"]), "--stdio"]

    client = LspClient(command, tmp_path, settings)
    owned_identities: list[tuple[int, float]] = []
    process_group: int | None = None
    try:
        client.start()
        assert client.process is not None
        process_group = client.process.pid
        client.request(
            "initialize",
            initialize_params(profile, locked["tsserver"] if engine == "typescript" else None),
            10.0,
        )
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": settings})
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path_uri(source),
                    "languageId": "typescript" if engine == "typescript" else "python",
                    "version": 1,
                    "text": source.read_text(encoding="utf-8"),
                }
            },
        )

        main = psutil.Process(client.process.pid)
        deadline = time.monotonic() + 5.0
        descendants: list[psutil.Process] = []
        while time.monotonic() < deadline:
            descendants = main.children(recursive=True)
            if engine == "pyright" or len(descendants) >= 2:
                break
            time.sleep(0.05)
        owned = [main, *descendants]
        owned_identities = [(process.pid, process.create_time()) for process in owned]

        if engine == "typescript":
            tsserver_children = [
                process
                for process in descendants
                if any(str(locked["tsserver"]) == argument for argument in process.cmdline())
            ]
            assert len(tsserver_children) == 2, [process.cmdline() for process in descendants]
        else:
            assert str(locked["pyright-langserver"]) in main.cmdline()

        terminate_process_tree_with_kill_fallback(
            client.process,
            2.0,
            f"real {engine} language server",
            kill_timeout=2.0,
        )

        assert all(not _same_live_process(*identity) for identity in owned_identities)
    finally:
        client.close()
        if process_group is not None:
            _kill_group_if_identity_is_still_a_member(process_group, owned_identities)
