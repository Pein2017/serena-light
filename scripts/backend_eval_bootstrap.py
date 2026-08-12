"""Closed startup shim for the backend evaluator transport bootstrap."""

from __future__ import annotations

import os
import sys

_CONTEXT_NAME = "_serena_light_backend_eval_outer_bootstrap"


def _closed_startup() -> bool:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    prefix = os.path.realpath(sys.base_prefix)
    expected_path = [
        os.path.join(prefix, "lib", f"python{sys.version_info.major}{sys.version_info.minor}.zip"),
        os.path.join(prefix, "lib", version),
        os.path.join(prefix, "lib", version, "lib-dynload"),
    ]
    return (
        sys.flags.isolated == 1
        and sys.flags.no_site == 1
        and sys.flags.dont_write_bytecode == 1
        and sys.dont_write_bytecode
        and [os.path.realpath(entry) for entry in sys.path] == expected_path
    )


if not _closed_startup():
    sys.stderr.write(
        "backend evaluation requires closed CPython startup: "
        "python -I -S -B scripts/backend_eval_bootstrap.py\n"
    )
    raise SystemExit(2)

import runpy  # noqa: E402
from pathlib import Path  # noqa: E402

_SHIM = Path(os.path.realpath(__file__))
_ADMISSION = _SHIM.with_name("backend_eval") / "admission.py"
_LOADER_PATH = os.path.realpath(getattr(__loader__, "path", ""))
if str(_SHIM) != _LOADER_PATH or os.path.realpath(sys.argv[0]) != str(_SHIM):
    sys.stderr.write("backend evaluation direct bootstrap provenance is invalid\n")
    raise SystemExit(2)

_context = type(sys)(_CONTEXT_NAME)
_context.__dict__.update(
    shim_path=str(_SHIM),
    admission_path=str(_ADMISSION),
    loader_path=_LOADER_PATH,
    argv_tail=tuple(sys.argv[1:]),
    capability=object(),
)
sys.modules[_CONTEXT_NAME] = _context
runpy.run_path(str(_ADMISSION), run_name="__main__")
