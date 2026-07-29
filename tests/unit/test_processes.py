"""Focused unit tests for owned process launch and normal cleanup."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from serena_light.processes import (
    LanguageServerSubprocessLauncher,
    _process_group_has_live_members,
    terminate_process_tree_with_kill_fallback,
)

ROOT = Path(__file__).resolve().parents[2]
type SymbolNode = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _read_line(process: subprocess.Popen[bytes], timeout: float = 5.0) -> str:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), f"pid={process.pid} produced no output before timeout"
        return process.stdout.readline().decode().strip()
    finally:
        selector.close()


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _kill_owned_group_if_still_present(process_group: int, identities: list[tuple[int, float]]) -> None:
    for pid, create_time in identities:
        try:
            candidate = psutil.Process(pid)
            if candidate.create_time() == create_time and os.getpgid(pid) == process_group:
                os.killpg(process_group, 9)
                return
        except (OSError, psutil.Error):
            continue


def test_launcher_uses_one_persistent_spawner_and_an_owned_session(tmp_path) -> None:
    launcher = LanguageServerSubprocessLauncher.get_instance()
    process = launcher.launch(
        [sys.executable, "-c", "import time; print('READY', flush=True); time.sleep(60)"],
        cwd=tmp_path,
    )
    try:
        assert _read_line(process) == "READY"
        assert os.getsid(process.pid) == process.pid
        assert os.getpgid(process.pid) == process.pid
        spawners = [thread for thread in threading.enumerate() if thread.name == "serena-light-pdeathsig-spawner"]
        if sys.platform == "linux":
            assert len(spawners) == 1
            assert spawners[0].daemon
    finally:
        terminate_process_tree_with_kill_fallback(process, 1.0, "test server")
        _close_pipes(process)


def test_terminate_cleans_an_owned_process_group(tmp_path) -> None:
    child_source = "import time; time.sleep(60)"
    parent_source = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_source!r}]); "
        "print(child.pid,flush=True); time.sleep(60)"
    )
    process = LanguageServerSubprocessLauncher.get_instance().launch(
        [sys.executable, "-c", parent_source],
        cwd=tmp_path,
    )
    process_group = process.pid
    identities: list[tuple[int, float]] = [(process.pid, psutil.Process(process.pid).create_time())]
    try:
        child_pid = int(_read_line(process))
        identities.append((child_pid, psutil.Process(child_pid).create_time()))
        assert os.getpgid(child_pid) == process_group

        terminate_process_tree_with_kill_fallback(process, 1.0, "graceful tree")

        assert process.poll() is not None
        assert not _process_group_has_live_members(process_group)
    finally:
        _kill_owned_group_if_still_present(process_group, identities)
        _close_pipes(process)


def test_kill_fallback_cleans_a_term_resistant_process_group(tmp_path) -> None:
    child_source = (
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('READY',flush=True); time.sleep(60)"
    )
    parent_source = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"child=subprocess.Popen([sys.executable,'-c',{child_source!r}],stdout=subprocess.PIPE,text=True); "
        "assert child.stdout and child.stdout.readline().strip() == 'READY'; "
        "print(child.pid,flush=True); time.sleep(60)"
    )
    process = LanguageServerSubprocessLauncher.get_instance().launch(
        [sys.executable, "-c", parent_source],
        cwd=tmp_path,
    )
    process_group = process.pid
    identities: list[tuple[int, float]] = [(process.pid, psutil.Process(process.pid).create_time())]
    try:
        child_pid = int(_read_line(process))
        identities.append((child_pid, psutil.Process(child_pid).create_time()))
        assert os.getpgid(child_pid) == process_group

        started = time.monotonic()
        terminate_process_tree_with_kill_fallback(
            process,
            0.1,
            "term-resistant tree",
            kill_timeout=2.0,
        )

        assert time.monotonic() - started >= 0.08
        assert process.poll() is not None
        assert not _process_group_has_live_members(process_group)
    finally:
        _kill_owned_group_if_still_present(process_group, identities)
        _close_pipes(process)


def test_cleanup_never_signals_the_callers_process_group() -> None:
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert os.getpgid(target.pid) == os.getpgrp()
        assert os.getpgid(sentinel.pid) == os.getpgrp()

        terminate_process_tree_with_kill_fallback(target, 1.0, "non-session child")

        assert target.poll() is not None
        assert sentinel.poll() is None
    finally:
        for process in (target, sentinel):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2.0)


@pytest.mark.parametrize("terminate_timeout,kill_timeout", [(-0.1, 1.0), (1.0, -0.1)])
def test_cleanup_rejects_negative_timeouts(terminate_timeout: float, kill_timeout: float) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=2.0)

    with pytest.raises(ValueError, match="non-negative"):
        terminate_process_tree_with_kill_fallback(
            process,
            terminate_timeout,
            kill_timeout=kill_timeout,
        )


def test_copied_source_provenance_hashes_match_pinned_upstream_symbols() -> None:
    manifest = json.loads((ROOT / "third_party" / "copied_sources.json").read_text(encoding="utf-8"))
    assert {
        entry["source_symbol"]
        for entry in manifest["copies"]
        if entry["local_owner"] == "src/serena_light/processes.py"
    } == {
        "LanguageServerSubprocessLauncher",
        "_signal_process_tree",
        "terminate_process_tree_with_kill_fallback",
    }

    reference = Path(manifest["reference"]["repository"])
    source_cache: dict[Path, tuple[list[str], dict[str, SymbolNode]]] = {}
    for entry in manifest["copies"]:
        source = reference / entry["source_path"]
        if source not in source_cache:
            source_text = source.read_text(encoding="utf-8")
            symbol_nodes: dict[str, SymbolNode] = {}
            for node in ast.parse(source_text).body:
                if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                symbol_nodes[node.name] = node
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                            symbol_nodes[f"{node.name}.{child.name}"] = child
            source_cache[source] = (source_text.splitlines(keepends=True), symbol_nodes)
        source_lines, symbol_nodes = source_cache[source]
        node = symbol_nodes[entry["source_symbol"]]
        assert node.end_lineno is not None
        fragment = "".join(source_lines[node.lineno - 1 : node.end_lineno])
        assert hashlib.sha256(fragment.encode()).hexdigest() == entry["copied_sha256"]
        assert (ROOT / entry["local_owner"]).is_file()
