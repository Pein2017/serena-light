"""Shared helpers for building the execution expectation these tests run under.

Every production-helper call now takes a
:class:`~scripts.backend_eval.source_binding.HelperExpectation`: the exact child-program and
helper-closure digests one run requires, derived from the evaluator identity that run
captured.  Tests need two flavours of it -- the real one for this checkout, and a synthetic
one for a temporary checkout a test built itself -- and neither belongs in the evaluator,
where a "read whatever is on disk now" constructor would be exactly the first-use pin this
repair removed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from scripts.backend_eval.identity import capture_evaluator_identity
from scripts.backend_eval.models import sha256_bytes
from scripts.backend_eval.source_binding import (
    CHILD_EXECUTED_HELPERS,
    EVALUATION_OWNER_ROOT,
    PRODUCTION_CHILD_RELPATH,
    HelperExpectation,
)

__all__ = ["expectation_for", "real_expectation"]


@lru_cache(maxsize=1)
def real_expectation() -> HelperExpectation:
    """The expectation this checkout's own captured evaluator identity produces.

    Cached: an identity capture digests the whole evaluator closure and the CLI host
    interpreter binary, and every test in this package would otherwise repeat it.
    """

    return HelperExpectation.from_identity(capture_evaluator_identity(), owner_root=EVALUATION_OWNER_ROOT)


def expectation_for(owner_root: Path) -> HelperExpectation:
    """The expectation naming the bytes a synthetic checkout currently holds.

    A test that builds a temporary evaluator checkout is the authority for what that
    checkout's identity would have recorded, so it states it explicitly here.
    """

    return HelperExpectation(
        owner_root=owner_root,
        child_digest=sha256_bytes((owner_root / PRODUCTION_CHILD_RELPATH).read_bytes()),
        closure=tuple(
            (relative, sha256_bytes((owner_root / relative).read_bytes()))
            for relative in CHILD_EXECUTED_HELPERS
        ),
    )
