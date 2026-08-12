"""Closed startup shim for the backend evaluator transport bootstrap."""

from __future__ import annotations

import runpy
from pathlib import Path

_ADMISSION = Path(__file__).with_name("backend_eval") / "admission.py"
runpy.run_path(str(_ADMISSION), run_name="__main__")
