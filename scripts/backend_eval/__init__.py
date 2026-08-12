"""Evaluation-only Python-backend evaluation harness (never imported by src/serena_light).

Importing this package binds ``serena_light`` to the production source of *this* evaluator
checkout before any helper is imported.  The CLI host interpreter is allowed to be a
foreign virtual environment -- the recorded deviation runs one -- and such an environment
carries an editable ``.pth`` that points at whichever checkout installed it.  Without this
bootstrap the evaluator would execute the semantic helpers of *another* worktree while its
receipt named only ``scripts/backend_eval``.  The path entry goes in front of every ambient
entry, and :mod:`scripts.backend_eval.source_binding` fails closed if a ``serena_light``
module still resolves outside this checkout.
"""

from __future__ import annotations

import os
import sys

# The sealed command image has a zip-shaped ``__file__``; its bootstrap supplies the disk
# owner explicitly before importing this package.  Ordinary imports retain the path-derived
# checkout binding.
_IMAGE_ACTIVE = os.environ.get("SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_ACTIVE") == "1"
_CONFIGURED_OWNER = os.environ.get("SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT") if _IMAGE_ACTIVE else None
if _CONFIGURED_OWNER is not None:
    if not os.path.isabs(_CONFIGURED_OWNER):
        raise RuntimeError(f"sealed evaluator owner root must be absolute: {_CONFIGURED_OWNER}")
    _EVALUATION_OWNER_ROOT = _CONFIGURED_OWNER
else:
    # <owner-root>/scripts/backend_eval/__init__.py -> <owner-root>
    _EVALUATION_OWNER_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_PRODUCTION_SOURCE_ROOT = os.path.join(_EVALUATION_OWNER_ROOT, "src")

if not sys.path or sys.path[0] != _PRODUCTION_SOURCE_ROOT:
    while _PRODUCTION_SOURCE_ROOT in sys.path:
        sys.path.remove(_PRODUCTION_SOURCE_ROOT)
    sys.path.insert(0, _PRODUCTION_SOURCE_ROOT)
