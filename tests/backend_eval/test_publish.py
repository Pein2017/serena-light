"""The generic atomic immutable publication primitive, on its own terms.

``publish.py`` is the low-level half of what ``admission._publish_receipt`` used to be: the
``O_EXCL`` temporary, the atomic ``link`` that never replaces an existing entry, the
per-target ``O_NOFOLLOW`` lock, the deadline-checked write chunks, the publication reserve in
front of the link, and the post-link checkpoint after every namespace mutation and durability
barrier.  Nothing here knows what a receipt means.

These tests are deliberately the *primitive's* characterization.  Admission's translation of
the two typed codes -- and the fact that its published text is unchanged -- is pinned in
``test_admission.py``; what is pinned here is the behaviour that text describes, plus the
reusability a second phase depends on: a different directory, lock, noun, and step prefix
publish under the same guarantees and cannot collide with Phase 1's names.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import inspect
import itertools
import os
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from scripts.backend_eval.process import Deadline
from scripts.backend_eval.publish import (
    PUBLICATION_DEADLINE_EXCEEDED,
    PUBLICATION_FAILED,
    PUBLICATION_RESERVE_SECONDS,
    PublicationError,
    PublicationFailure,
    PublicationRequest,
    _Publication,
    publish_immutable_record,
)

RUN = "a" * 64
OTHER_RUN = "b" * 64
PAYLOAD = b'{"kind":"record"}'


@dataclass(slots=True)
class FakeClock:
    """A monotonic clock a test advances by an exact amount; ``drift`` charges each read."""

    now: float = 0.0
    drift: float = 0.0

    def __call__(self) -> float:
        self.now += self.drift
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def owner_root(tmp_path: Path) -> Path:
    root = tmp_path / "owner"
    root.mkdir()
    return root


def _request(owner_root: Path, **overrides: object) -> PublicationRequest:
    """Phase 1's exact publication shape, so a default here is the shape admission uses."""

    base = PublicationRequest(
        owner_root=owner_root,
        target_root=owner_root / ".admission-artifacts" / "backend-eval" / ("e" * 64),
        directory_name="receipts",
        lock_name=".admission-publication.lock",
        identity=RUN,
        entry_name=f"{RUN}.json",
        temporary_name=f".{RUN}.json.tmp",
        payload=PAYLOAD,
    )
    return replace(base, **overrides)


def _deadline(clock: FakeClock, seconds: float = 100.0) -> Deadline:
    return Deadline(clock=clock, seconds=seconds, started=0.0)


def _entries(request: PublicationRequest) -> list[str]:
    return sorted(entry.name for entry in (request.target_root / request.directory_name).iterdir())


# --- the published record ---------------------------------------------------------


def test_a_record_is_published_at_the_returned_path_with_owned_modes(
    owner_root: Path, clock: FakeClock
) -> None:
    """One publication, one entry, and every component the primitive created is service-owned."""

    request = _request(owner_root)
    published = publish_immutable_record(request, _deadline(clock))

    assert published == request.target_root / "receipts" / f"{RUN}.json"
    assert published.read_bytes() == PAYLOAD
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert stat.S_IMODE(published.parent.stat().st_mode) == 0o700
    # Every intermediate component the walk created, not just the leaf directory.
    assert stat.S_IMODE(request.target_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((owner_root / ".admission-artifacts").stat().st_mode) == 0o700
    lock = request.target_root / request.lock_name
    assert lock.is_file()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    # The temporary is gone and its unlink was synced before the primitive returned.
    assert _entries(request) == [f"{RUN}.json"]


def test_an_existing_entry_is_never_replaced_even_by_the_same_identity(
    owner_root: Path, clock: FakeClock
) -> None:
    """The link is the immutability: a repeated identity is refused, not overwritten."""

    request = _request(owner_root)
    published = publish_immutable_record(request, _deadline(clock))

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(replace(request, payload=b"replacement"), _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == f"a receipt for run {RUN} already exists and is immutable"
    assert published.read_bytes() == PAYLOAD
    # The refused call published nothing and left nothing.  The owning phase's cleanup runs
    # *before* publication, so a temporary abandoned here would never be collected at all.
    assert _entries(request) == [f"{RUN}.json"]


def test_two_identities_publish_side_by_side_without_touching_each_other(
    owner_root: Path, clock: FakeClock
) -> None:
    request = _request(owner_root)
    other = _request(
        owner_root,
        identity=OTHER_RUN,
        entry_name=f"{OTHER_RUN}.json",
        temporary_name=f".{OTHER_RUN}.json.tmp",
        payload=b"second",
    )

    publish_immutable_record(request, _deadline(clock))
    publish_immutable_record(other, _deadline(clock))

    assert _entries(request) == sorted([f"{RUN}.json", f"{OTHER_RUN}.json"])
    assert (request.target_root / "receipts" / f"{RUN}.json").read_bytes() == PAYLOAD


def test_a_stale_temporary_is_cleared_rather_than_appended_to(owner_root: Path, clock: FakeClock) -> None:
    """An interrupted earlier attempt's dot-name must not become part of this payload."""

    request = _request(owner_root)
    directory = request.target_root / "receipts"
    directory.mkdir(parents=True)
    (directory / request.temporary_name).write_bytes(b"garbage from an interrupted run")

    published = publish_immutable_record(request, _deadline(clock))

    assert published.read_bytes() == PAYLOAD
    assert _entries(request) == [f"{RUN}.json"]


def test_a_payload_larger_than_one_chunk_is_written_whole(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunking is what keeps the ceiling observable; it must not truncate or reorder."""

    import scripts.backend_eval.publish as publish_module

    monkeypatch.setattr(publish_module, "_WRITE_CHUNK_BYTES", 7)
    payload = bytes(range(256)) * 5
    published = publish_immutable_record(_request(owner_root, payload=payload), _deadline(clock))

    assert published.read_bytes() == payload


# --- descriptor confinement -------------------------------------------------------


def test_a_symlinked_publication_directory_is_refused(owner_root: Path, clock: FakeClock) -> None:
    """``receipts`` is opened ``O_NOFOLLOW`` from its parent's descriptor, never by pathname."""

    request = _request(owner_root)
    decoy = owner_root.parent / "decoy"
    decoy.mkdir()
    request.target_root.mkdir(parents=True)
    (request.target_root / "receipts").symlink_to(decoy)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert "must be an evaluation-owned directory" in error.value.failure.detail
    assert list(decoy.iterdir()) == []


def test_a_symlinked_intermediate_component_is_refused(owner_root: Path, clock: FakeClock) -> None:
    """The guard is per component: a swapped ancestor cannot move the target either."""

    request = _request(owner_root)
    decoy = owner_root.parent / "decoy-tree"
    decoy.mkdir()
    (owner_root / ".admission-artifacts").symlink_to(decoy)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert "must be an evaluation-owned directory" in error.value.failure.detail
    assert list(decoy.iterdir()) == []


def test_a_symlink_planted_at_the_final_name_is_not_followed(owner_root: Path, clock: FakeClock) -> None:
    """``link(..., follow_symlinks=False)`` refuses the name instead of writing through it."""

    request = _request(owner_root)
    decoy = owner_root.parent / "decoy.json"
    decoy.write_bytes(b"untouched")
    directory = request.target_root / "receipts"
    directory.mkdir(parents=True)
    (directory / request.entry_name).symlink_to(decoy)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert "already exists and is immutable" in error.value.failure.detail
    assert decoy.read_bytes() == b"untouched"
    # The planted symlink is refused, never resolved or replaced, and nothing is left beside it.
    assert _entries(request) == [request.entry_name]
    assert (request.target_root / "receipts" / request.entry_name).is_symlink()


def test_the_temporary_is_created_exclusively_and_without_truncation() -> None:
    """Structural pin: the create flags are the ones the immutability argument rests on."""

    import scripts.backend_eval.publish as publish_module

    assert publish_module._CREATE_FLAGS & os.O_EXCL
    assert publish_module._CREATE_FLAGS & os.O_CREAT
    assert publish_module._CREATE_FLAGS & os.O_NOFOLLOW
    assert not publish_module._CREATE_FLAGS & os.O_TRUNC
    assert publish_module._FILE_MODE == 0o600
    assert publish_module._DIRECTORY_MODE == 0o700
    assert publish_module._NOFOLLOW_DIRECTORY_FLAGS & os.O_NOFOLLOW
    assert publish_module._NOFOLLOW_DIRECTORY_FLAGS & os.O_DIRECTORY


# --- the ceiling ------------------------------------------------------------------


def _expire_after_the_lock(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, seconds: float
) -> None:
    """Advance the clock exactly once, after the lock and before the first write chunk.

    Driving expiry from a real step boundary is the only way to reach the post-lock checks
    deterministically: an already-expired deadline never gets past ``publish_receipt:lock``.
    """

    import scripts.backend_eval.publish as publish_module

    real = publish_module.acquire_exclusive_lock

    def acquire(fd: int, **kwargs: object) -> None:
        real(fd, **kwargs)  # type: ignore[arg-type]
        clock.advance(seconds)

    monkeypatch.setattr(publish_module, "acquire_exclusive_lock", acquire)


def test_an_expired_ceiling_refuses_before_the_first_write_chunk(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(owner_root)
    _expire_after_the_lock(monkeypatch, clock, 100.0)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_DEADLINE_EXCEEDED
    assert "step=publish_receipt:write" in error.value.failure.detail
    # The interrupted attempt leaves neither an entry nor a half-written temporary.
    assert _entries(request) == []


def test_the_link_is_refused_when_no_publication_reserve_remains(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reserve is the point: a link that cannot be completed is not started."""

    request = _request(owner_root)
    # Live, but with less than the reserve left: the write succeeds and the link must not.
    _expire_after_the_lock(monkeypatch, clock, 100.0 - PUBLICATION_RESERVE_SECONDS + 0.5)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_DEADLINE_EXCEEDED
    assert error.value.failure.detail == (
        "step=publish_receipt:link elapsed=95.500s budget=100s reserve=5s; the receipt was not published"
    )
    assert _entries(request) == []


def test_a_lock_held_by_another_process_cannot_carry_a_call_past_its_ceiling(
    owner_root: Path,
) -> None:
    """Waiting for another publication is bounded by the same ceiling, never by the holder."""

    request = _request(owner_root)
    request.target_root.mkdir(parents=True)
    holder = os.open(request.target_root / request.lock_name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    # Every clock read costs time, so the poll loop is charged without real waiting.
    started = time.monotonic()
    try:
        with pytest.raises(PublicationError) as error:
            publish_immutable_record(request, _deadline(FakeClock(drift=1.0)))
    finally:
        os.close(holder)

    assert error.value.failure.code == PUBLICATION_DEADLINE_EXCEEDED
    assert "step=publish_receipt:lock" in error.value.failure.detail
    assert time.monotonic() - started < 20.0
    assert not (request.target_root / request.directory_name).exists()


# --- post-link checkpoints --------------------------------------------------------

# The publication steps that move the namespace or force durability after the atomic link.
# Each one has to be *followed* by a ceiling observation, or a call can earn its published
# record with work it did after the ceiling -- which is exactly the defect this pins closed.
_POST_LINK_OPERATIONS = ("_sync_directory", "_replace_temporary")


def _publication_step_order() -> tuple[str, ...]:
    """The post-link operations and checkpoints of the primitive, in source order."""

    import scripts.backend_eval.publish as publish_module

    module = ast.parse(inspect.getsource(publish_module))
    publish = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "publish_immutable_record"
    )
    link = next(
        node.lineno
        for node in ast.walk(publish)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "link"
    )
    steps: list[tuple[int, str]] = []
    for node in ast.walk(publish):
        if not isinstance(node, ast.Call) or node.lineno <= link:
            continue
        if isinstance(node.func, ast.Name) and node.func.id in _POST_LINK_OPERATIONS:
            steps.append((node.lineno, node.func.id))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "checkpoint":
            steps.append((node.lineno, "checkpoint"))
    return tuple(name for _, name in sorted(steps))


def test_no_post_link_publication_step_is_left_unchecked() -> None:
    """Structural pin: after the link, no mutation or barrier may be the last word.

    Behaviour tests can only slow the barriers that exist today.  This asserts the shape the
    whole argument rests on: every post-link namespace mutation and durability barrier is
    immediately followed by a ceiling observation, and the very last statement before the
    return is one too -- so there is no step whose cost a later success can absorb.
    """

    steps = _publication_step_order()

    assert steps.count("checkpoint") == 4
    assert steps[-1] == "checkpoint", steps
    assert all(
        later == "checkpoint" for earlier, later in itertools.pairwise(steps) if earlier != "checkpoint"
    ), steps
    assert steps == (
        "_sync_directory",
        "checkpoint",
        "_replace_temporary",
        "checkpoint",
        "_sync_directory",
        "checkpoint",
        "checkpoint",
    )


def test_a_checkpoint_inside_the_ceiling_keeps_the_published_record(
    owner_root: Path, clock: FakeClock
) -> None:
    """The withdrawal is driven by expiry alone; a live deadline never touches the link."""

    request = _request(owner_root)
    published = publish_immutable_record(request, _deadline(clock))

    directory_fd = os.open(published.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _Publication(directory_fd, request, _deadline(clock)).checkpoint("return")
    finally:
        os.close(directory_fd)

    assert published.is_file()


@pytest.mark.parametrize("step", ["link_synced", "temporary_unlinked", "temporary_unlink_synced", "return"])
def test_a_record_published_exactly_at_the_ceiling_is_withdrawn(
    owner_root: Path, clock: FakeClock, step: str
) -> None:
    """Every post-link checkpoint withdraws this call's own link, the last one included.

    The final checkpoint sits immediately before the return, where only descriptor closes
    remain, so it is the one that decides whether an overrun can still be returned as a
    published record -- it must withdraw exactly like the earlier ones.
    """

    request = _request(owner_root)
    published = publish_immutable_record(request, _deadline(clock))
    temporary = published.parent / request.temporary_name
    temporary.write_bytes(b"{}")

    directory_fd = os.open(published.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expired = Deadline.start(clock, 10.0)
        clock.advance(11.0)
        with pytest.raises(PublicationError) as error:
            _Publication(directory_fd, request, expired).checkpoint(step)
    finally:
        os.close(directory_fd)

    assert error.value.failure.code == PUBLICATION_DEADLINE_EXCEEDED
    assert f"step=publish_receipt:{step}" in error.value.failure.detail
    assert "the receipt was withdrawn and none was published" in error.value.failure.detail
    assert not published.exists()
    assert not temporary.exists()


def test_a_withdrawal_removes_this_calls_own_names_and_no_others(
    owner_root: Path, clock: FakeClock
) -> None:
    """Immutability survives the withdrawal path: another identity's entry is untouched."""

    request = _request(owner_root)
    other = _request(
        owner_root,
        identity=OTHER_RUN,
        entry_name=f"{OTHER_RUN}.json",
        temporary_name=f".{OTHER_RUN}.json.tmp",
        payload=b"second",
    )
    published = publish_immutable_record(request, _deadline(clock))
    survivor = publish_immutable_record(other, _deadline(clock))

    directory_fd = os.open(published.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expired = Deadline.start(clock, 10.0)
        clock.advance(11.0)
        with pytest.raises(PublicationError):
            _Publication(directory_fd, request, expired).checkpoint("return")
    finally:
        os.close(directory_fd)

    assert not published.exists()
    assert survivor.read_bytes() == b"second"


@pytest.mark.parametrize(("boundary", "step"), [(1, "link_synced"), (2, "temporary_unlink_synced")])
def test_a_slow_post_link_directory_sync_publishes_nothing(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, boundary: int, step: str
) -> None:
    """Delay a post-link ``fsync`` and the published record must not survive it."""

    import scripts.backend_eval.publish as publish_module

    request = _request(owner_root)
    real_sync = publish_module._sync_directory
    calls = {"n": 0}

    def slow_sync(dir_fd: int, publication: PublicationRequest) -> None:
        calls["n"] += 1
        if calls["n"] == boundary:
            clock.advance(1000.0)
        real_sync(dir_fd, publication)

    monkeypatch.setattr(publish_module, "_sync_directory", slow_sync)
    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_DEADLINE_EXCEEDED
    assert f"step=publish_receipt:{step}" in error.value.failure.detail
    assert _entries(request) == []


def test_a_slow_post_link_temporary_unlink_publishes_nothing(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace mutation after the link is a checkpoint boundary too, not just a sync."""

    import scripts.backend_eval.publish as publish_module

    request = _request(owner_root)
    real_replace = publish_module._replace_temporary
    seen: list[str] = []

    def slow_replace(dir_fd: int, publication: PublicationRequest) -> None:
        seen.append(publication.temporary_name)
        real_replace(dir_fd, publication)
        # Only the post-link unlink is slowed; the pre-write one must stay inside the budget.
        if len(seen) == 2:
            clock.advance(1000.0)

    monkeypatch.setattr(publish_module, "_replace_temporary", slow_replace)
    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert "step=publish_receipt:temporary_unlinked" in error.value.failure.detail
    assert _entries(request) == []


# --- reuse by a second phase ------------------------------------------------------


def test_a_second_phase_publishes_under_its_own_names_and_vocabulary(
    owner_root: Path, clock: FakeClock
) -> None:
    """The reuse Task 8 needs: another directory, lock, noun, and step prefix, same guarantees."""

    target = owner_root / ".admission-artifacts" / "backend-eval" / ("e" * 64)
    admission = _request(owner_root, target_root=target)
    phase2 = PublicationRequest(
        owner_root=owner_root,
        target_root=target,
        directory_name="protocol-receipts",
        lock_name=".protocol-publication.lock",
        identity=RUN,
        entry_name=f"{RUN}.json",
        temporary_name=f".{RUN}.json.tmp",
        payload=b'{"kind":"protocol"}',
        noun="protocol receipt",
        step_prefix="publish_protocol_receipt",
    )

    admission_path = publish_immutable_record(admission, _deadline(clock))
    phase2_path = publish_immutable_record(phase2, _deadline(clock))

    # Same identity, same evaluation root, two independent immutable publications.
    assert admission_path != phase2_path
    assert admission_path.read_bytes() == PAYLOAD
    assert phase2_path.read_bytes() == b'{"kind":"protocol"}'
    assert stat.S_IMODE(phase2_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(phase2_path.parent.stat().st_mode) == 0o700
    assert (target / ".protocol-publication.lock").is_file()
    assert _entries(phase2) == [f"{RUN}.json"]


def test_a_second_phase_failure_speaks_its_own_vocabulary(owner_root: Path, clock: FakeClock) -> None:
    """The noun and the step prefix are the request's, so no phase inherits another's text."""

    phase2 = _request(
        owner_root,
        directory_name="protocol-receipts",
        lock_name=".protocol-publication.lock",
        noun="protocol receipt",
        step_prefix="publish_protocol_receipt",
    )
    publish_immutable_record(phase2, _deadline(clock))

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(phase2, _deadline(clock))
    assert error.value.failure.detail == f"a protocol receipt for run {RUN} already exists and is immutable"

    directory_fd = os.open(phase2.target_root / "protocol-receipts", os.O_RDONLY | os.O_DIRECTORY)
    try:
        expired = Deadline.start(clock, 10.0)
        clock.advance(11.0)
        with pytest.raises(PublicationError) as withdrawn:
            _Publication(directory_fd, phase2, expired).checkpoint("return")
    finally:
        os.close(directory_fd)
    assert "step=publish_protocol_receipt:return" in withdrawn.value.failure.detail
    assert "the protocol receipt was withdrawn and none was published" in withdrawn.value.failure.detail


# --- the request contract ---------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"directory_name": "a/b"},
        {"directory_name": ".."},
        {"directory_name": ""},
        {"lock_name": "../escape.lock"},
        {"entry_name": f"receipts/{RUN}.json"},
        {"temporary_name": "."},
    ],
)
def test_a_pathname_shaped_name_is_refused(owner_root: Path, overrides: dict[str, str]) -> None:
    """Every name is handed to a ``dir_fd`` call, so a name with a separator is a pathname."""

    with pytest.raises(ValueError, match="single path component"):
        _request(owner_root, **overrides)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"owner_root": Path("relative")}, "must be an absolute path"),
        ({"target_root": Path("/data/x/../y")}, "must not contain parent references"),
        ({"temporary_name": f"{RUN}.json"}, "must differ from entry_name"),
        ({"identity": ""}, "identity must be a non-empty string"),
        ({"noun": ""}, "noun must be a non-empty string"),
        ({"step_prefix": ""}, "step_prefix must be a non-empty string"),
    ],
)
def test_an_incoherent_request_is_refused(owner_root: Path, overrides: dict[str, object], match: str) -> None:
    root = overrides.pop("owner_root", owner_root)
    assert isinstance(root, Path)
    with pytest.raises(ValueError, match=match):
        _request(root, **overrides)


@pytest.mark.parametrize("code", ["", "receipt_publication_failed", "deadline"])
def test_only_the_two_declared_codes_are_publishable(code: str) -> None:
    """The adapter's translation table is total only because this set is closed."""

    with pytest.raises(ValueError, match="PublicationFailure.code must be"):
        PublicationFailure(code=code, detail="detail")


def test_a_failure_carries_its_code_and_detail_in_the_message() -> None:
    error = PublicationError(PublicationFailure(code=PUBLICATION_FAILED, detail="cannot lock /data/x"))

    assert error.failure.code == PUBLICATION_FAILED
    assert str(error) == "publication_failed: cannot lock /data/x"


# --- the exact published text -----------------------------------------------------
#
# Phase 1's failure text is part of its receipt contract, so the extraction is only
# behaviour-preserving if the rendered strings are the historical ones.  Each of these
# induces a real failure under admission's own request and asserts the whole detail, not a
# substring: a reworded template fails here rather than in a receipt someone reads later.


def test_an_unreachable_owner_root_reports_the_historical_text(tmp_path: Path, clock: FakeClock) -> None:
    missing = tmp_path / "missing-owner"
    request = _request(missing, target_root=missing / "e")

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"cannot open {missing}: [Errno 2] No such file or directory: '{missing}'"
    )


def test_a_regular_file_where_a_component_belongs_reports_the_historical_text(
    owner_root: Path, clock: FakeClock
) -> None:
    request = _request(owner_root)
    request.target_root.mkdir(parents=True)
    (request.target_root / "receipts").write_bytes(b"not a directory")

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.detail == (
        "artifact component 'receipts' must be an evaluation-owned directory: "
        "[Errno 20] Not a directory: 'receipts'"
    )


def test_a_symlinked_publication_lock_reports_the_historical_text(
    owner_root: Path, clock: FakeClock
) -> None:
    request = _request(owner_root)
    request.target_root.mkdir(parents=True)
    (request.target_root / request.lock_name).symlink_to(owner_root.parent / "elsewhere.lock")

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.detail == (
        f"cannot open the publication lock below {request.target_root}: "
        f"[Errno 40] Too many levels of symbolic links: '{request.lock_name}'"
    )


def test_an_unremovable_temporary_reports_the_historical_text(owner_root: Path, clock: FakeClock) -> None:
    request = _request(owner_root)
    directory = request.target_root / "receipts"
    directory.mkdir(parents=True)
    # A directory squatting on this run's dot-name cannot be unlinked away.
    (directory / request.temporary_name).mkdir()

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.detail == (
        f"cannot clear the receipt temporary below {request.target_root}: "
        f"[Errno 21] Is a directory: '{request.temporary_name}'"
    )


def test_an_unwithdrawable_link_reports_the_historical_text(owner_root: Path, clock: FakeClock) -> None:
    """The withdrawal path has its own text, and it names the entry it could not remove."""

    request = _request(owner_root, entry_name="blocked", temporary_name=".blocked.tmp")
    directory = request.target_root / "receipts"
    directory.mkdir(parents=True)
    (directory / "blocked").mkdir()

    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expired = Deadline.start(clock, 10.0)
        clock.advance(11.0)
        with pytest.raises(PublicationError) as error:
            _Publication(directory_fd, request, expired).checkpoint("return")
    finally:
        os.close(directory_fd)

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"the ceiling was reached during publication and blocked below {request.target_root} "
        "could not be withdrawn: [Errno 21] Is a directory: 'blocked'"
    )


# --- a post-link step that fails --------------------------------------------------
#
# The ceiling is not the only way a post-link step can end badly.  A durability barrier or
# the temporary unlink can fail outright, and until this was closed the typed failure
# propagated while the canonical final name stayed on disk -- a publication that reported
# failure and left readable evidence behind.  Every post-link step is now inside the same
# owned-name withdrawal the ceiling uses.


def _failing(monkeypatch: pytest.MonkeyPatch, name: str, call: int, detail: str) -> None:
    """Make the ``call``-th invocation of one publication step fail with a typed error."""

    import scripts.backend_eval.publish as publish_module

    real = getattr(publish_module, name)
    seen = {"n": 0}

    def wrapper(dir_fd: int, publication: PublicationRequest) -> None:
        seen["n"] += 1
        if seen["n"] == call:
            raise PublicationError(PublicationFailure(code=PUBLICATION_FAILED, detail=detail))
        real(dir_fd, publication)

    monkeypatch.setattr(publish_module, name, wrapper)


@pytest.mark.parametrize("call", [1, 2])
def test_a_failing_post_link_directory_sync_leaves_no_published_record(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    """Both post-link barriers: a failed ``fsync`` must not leave the canonical name behind.

    A publication that reaches the link performs exactly two barriers: the one right after
    the link and the one after the temporary is unlinked.  Either can fail, and neither may
    carry a readable canonical name out with it.
    """

    request = _request(owner_root)
    _failing(monkeypatch, "_sync_directory", call, "cannot synchronize the receipts directory below /x: boom")

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    # The original typed failure survives the withdrawal unchanged.
    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == "cannot synchronize the receipts directory below /x: boom"
    assert _entries(request) == []


def test_a_failing_post_link_temporary_unlink_leaves_no_published_record(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-link namespace mutation is inside the withdrawal too, not just the barriers."""

    request = _request(owner_root)
    _failing(
        monkeypatch,
        "_replace_temporary",
        2,
        f"cannot clear the receipt temporary below {request.target_root}: boom",
    )

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"cannot clear the receipt temporary below {request.target_root}: boom"
    )
    assert _entries(request) == []


def test_a_post_link_failure_withdraws_only_this_calls_own_names(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Immutability is not traded for the fix: another identity's record is untouched."""

    request = _request(owner_root)
    other = _request(
        owner_root,
        identity=OTHER_RUN,
        entry_name=f"{OTHER_RUN}.json",
        temporary_name=f".{OTHER_RUN}.json.tmp",
        payload=b"second",
    )
    survivor = publish_immutable_record(other, _deadline(clock))

    _failing(monkeypatch, "_sync_directory", 2, "boom")
    with pytest.raises(PublicationError):
        publish_immutable_record(request, _deadline(clock))

    assert _entries(request) == [f"{OTHER_RUN}.json"]
    assert survivor.read_bytes() == b"second"


def test_a_post_link_failure_whose_withdrawal_cannot_remove_the_entry_is_typed(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the entry cannot be withdrawn, that -- not the original -- is what is reported.

    Silently re-raising the original failure would claim nothing was published while the
    canonical name is still readable.  The caller is told exactly which name it could not
    withdraw, so the untrustworthy state is named rather than implied.
    """

    request = _request(owner_root)
    _failing(monkeypatch, "_sync_directory", 2, "boom")
    real_unlink = os.unlink

    def refuse_entry(name: object, *args: object, **kwargs: object) -> None:
        if name == request.entry_name:
            raise PermissionError(13, "Permission denied")
        real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", refuse_entry)
    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))
    monkeypatch.undo()

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"the receipt publication failed and {request.entry_name} below {request.target_root} "
        "could not be withdrawn: [Errno 13] Permission denied"
    )


def test_a_withdrawal_that_cannot_be_proven_durable_is_typed(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unproven withdrawal is reported as one, not as the failure that triggered it."""

    request = _request(owner_root)
    # Call 2 is the post-link barrier; call 3 is the withdrawal's own durability barrier.
    _failing(monkeypatch, "_sync_directory", 2, "boom")
    _failing(monkeypatch, "_sync_directory", 3, "cannot synchronize the receipts directory: gone")

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"the receipt publication failed and the withdrawal of the receipt below {request.target_root} "
        "could not be proven durable: cannot synchronize the receipts directory: gone"
    )


def test_the_ceiling_withdrawal_still_reports_the_ceiling_after_a_failed_barrier(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery must not re-withdraw or re-word an expiry the checkpoint already handled."""

    import scripts.backend_eval.publish as publish_module

    request = _request(owner_root)
    real_sync = publish_module._sync_directory
    calls = {"n": 0}

    def slow_sync(dir_fd: int, publication: PublicationRequest) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            clock.advance(1000.0)
        real_sync(dir_fd, publication)

    monkeypatch.setattr(publish_module, "_sync_directory", slow_sync)
    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_DEADLINE_EXCEEDED
    assert "step=publish_receipt:link_synced" in error.value.failure.detail
    assert "the receipt was withdrawn and none was published" in error.value.failure.detail
    # Exactly one withdrawal: the checkpoint's.  The recovery saw it and re-raised.
    assert _entries(request) == []


def test_every_post_link_step_is_inside_the_withdrawal_recovery() -> None:
    """Structural pin: the post-link block is guarded, and the guard withdraws.

    The behaviour tests can only fail the steps that exist today.  This asserts the shape
    the guarantee rests on: every post-link operation and checkpoint sits inside one ``try``
    whose handler catches the primitive's own typed failure and hands it to the recovery.
    """

    import scripts.backend_eval.publish as publish_module

    module = ast.parse(inspect.getsource(publish_module))
    publish = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "publish_immutable_record"
    )
    guards = [
        node
        for node in ast.walk(publish)
        if isinstance(node, ast.Try) and node.handlers
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "checkpoint"
            for call in ast.walk(node)
        )
    ]
    assert len(guards) == 1, "the post-link steps must share exactly one recovery guard"
    (handler,) = guards[0].handlers
    assert isinstance(handler.type, ast.Name) and handler.type.id == "PublicationError"
    assert [ast.unparse(node) for node in handler.body] == ["published.recover(exc)"]
    # And every post-link step is in that guard's body, not outside it.
    assert _publication_step_order() == tuple(
        name
        for name in (
            ast.unparse(node.value.func).split(".")[-1]
            for node in guards[0].body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        )
        if name in {*_POST_LINK_OPERATIONS, "checkpoint"}
    )


def test_a_target_root_outside_the_owner_root_is_a_typed_publication_failure(
    owner_root: Path, clock: FakeClock
) -> None:
    """Containment is checked lexically and reported typed, before any syscall runs.

    A target the declared owner root does not contain used to reach ``Path.relative_to`` and
    escape as a raw ``ValueError`` -- an untyped failure no phase adapter translates, and one
    a caller could not tell from a programming error inside the primitive.
    """

    outside = owner_root.parent / "elsewhere" / ("e" * 64)
    request = _request(owner_root, target_root=outside)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"the publication target {outside} must be the declared owner root {owner_root} or a path below it"
    )
    # Nothing was created on the way to refusing it.
    assert not outside.exists()


def test_the_owner_root_itself_is_an_accepted_target(owner_root: Path, clock: FakeClock) -> None:
    """The containment check accepts the degenerate case rather than over-refusing."""

    request = _request(owner_root, target_root=owner_root)

    assert publish_immutable_record(request, _deadline(clock)).read_bytes() == PAYLOAD


# --- the close boundary -----------------------------------------------------------
#
# ``close`` is the one syscall in this module whose failure carries no information the
# caller can act on, and it sits at four different points in the publication.  Before the
# link, a refused close is a real pre-link refusal: nothing is published, so the temporary
# must go and the reason must be the payload's own.  After the barriers, the record is
# published and durable, and a close that reports a deferred error must not be allowed to
# hand the caller a failure while the record it denies is on disk.


def _fail_close_of(monkeypatch: pytest.MonkeyPatch, match: object) -> None:
    """Let one descriptor's ``close`` report an error *after* the kernel released it.

    Linux frees the descriptor whether or not ``close`` returns an error -- the error is a
    deferred writeback report, not a signal that the descriptor is still open.  The injection
    therefore performs the real close first and only then raises, which is the state a real
    ``EIO`` at this boundary leaves behind.
    """

    real_open, real_close = os.open, os.close
    targets: set[int] = set()

    def open_(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if match(path, flags):  # type: ignore[operator]
            targets.add(fd)
        return fd

    def close_(fd: int) -> None:
        real_close(fd)
        if fd in targets:
            targets.discard(fd)
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "close", close_)


def _is_payload(path: object, flags: int) -> bool:
    import scripts.backend_eval.publish as publish_module

    return flags == publish_module._CREATE_FLAGS


def test_a_refused_payload_close_is_typed_as_the_payload_and_discards_the_temporary(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last step before the link is a real step, not an afterthought.

    A close that fails here used to escape the payload's own handling entirely and be
    relabelled by the publication lock's context manager as ``cannot lock`` -- a diagnosis
    naming a step that had already succeeded -- while this call's temporary stayed on disk
    for a cleanup that had already run.
    """

    request = _request(owner_root)
    _fail_close_of(monkeypatch, _is_payload)

    with pytest.raises(PublicationError) as error:
        publish_immutable_record(request, _deadline(clock))
    monkeypatch.undo()

    assert error.value.failure.code == PUBLICATION_FAILED
    assert error.value.failure.detail == (
        f"cannot close the receipt below {request.target_root}: [Errno 5] Input/output error"
    )
    assert "cannot lock" not in error.value.failure.detail
    # Nothing was published, and nothing was abandoned under a dot name.
    assert _entries(request) == []


@pytest.mark.parametrize(
    ("label", "match"),
    [
        ("publication directory", lambda path, flags: path == "receipts"),
        ("publication lock", lambda path, flags: path == ".admission-publication.lock"),
        ("owner root", lambda path, flags: isinstance(path, Path)),
    ],
)
def test_a_refused_close_after_durability_still_publishes_the_record(
    owner_root: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    match: object,
) -> None:
    """No close after the barriers may turn a durable publication into a failure.

    By the time these descriptors are released the link is made, the directory is synced,
    the temporary is gone and its removal is synced.  Reporting a deferred close error here
    would deny a record that is on disk and readable -- a caller told the publication failed
    while every consumer can see it succeeded.
    """

    del label
    request = _request(owner_root)
    _fail_close_of(monkeypatch, match)

    published = publish_immutable_record(request, _deadline(clock))
    monkeypatch.undo()

    assert published.read_bytes() == PAYLOAD
    assert _entries(request) == [f"{RUN}.json"]


def test_a_refused_close_during_the_owner_walk_still_publishes_the_record(
    owner_root: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk releases each parent once its child is open; that close decides nothing."""

    request = _request(owner_root)
    _fail_close_of(monkeypatch, lambda path, flags: path == ".admission-artifacts")

    published = publish_immutable_record(request, _deadline(clock))
    monkeypatch.undo()

    assert published.read_bytes() == PAYLOAD
    assert _entries(request) == [f"{RUN}.json"]


def test_the_publication_lock_never_relabels_a_failure_from_its_own_body() -> None:
    """Structural pin: only the lock's own open, ``fchmod``, and ``flock`` are its to name.

    The ``yield`` used to sit inside the handler that produces ``cannot lock``, so every
    ``OSError`` the publication raised under the lock -- none of which is a locking failure --
    was published under that name.  The body is now outside that handler entirely.
    """

    import scripts.backend_eval.publish as publish_module

    module = ast.parse(inspect.getsource(publish_module))
    lock = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_publication_lock"
    )
    relabelling = [
        node
        for node in ast.walk(lock)
        if isinstance(node, ast.Try)
        and any(
            isinstance(handler.type, ast.Name) and handler.type.id == "OSError"
            for handler in node.handlers
        )
    ]
    assert relabelling, "the lock must still name its own failures"
    for guard in relabelling:
        yields = [node for node in ast.walk(guard) if isinstance(node, ast.Yield)]
        assert yields == [], "the yielded body must not be inside an OSError relabelling guard"
