"""Finish against a recorded code identity (``lifecycle.resume_refusal``; #2588).

An interrupted record resumes only under the code and environment it was
launched with. A receipt written before the environment digest read the
installed packages (``digest_scheme`` absent) cannot show that no library
changed since, so it is refused with that reason rather than resumed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from app.research.persistence import lifecycle
from app.research.persistence.lifecycle import resume_refusal
from app.research.sweep.identity import (
    DIGEST_SCHEME,
    LEGACY_DIGEST_SCHEME,
    CodeIdentity,
    EnvironmentIdentityError,
)

CURRENT = CodeIdentity(git_revision="abc", tree_state="clean", source_digest="s" * 64, environment_digest="e" * 64, digest_scheme=DIGEST_SCHEME)


@dataclass(frozen=True)
class _Interrupted:
    """A fenced record Finish would otherwise run against: failed with cells missing, no live job."""

    receipt: dict[str, Any]
    status: str = "failed"
    job_id: str | None = None
    incomplete: bool = True


def _refusal(recorded: dict[str, Any]) -> str | None:
    return resume_refusal(_Interrupted(receipt={"code_identity": recorded}), noun="search", unit="cell", live=False, identity=CURRENT)


def test_the_same_code_and_environment_resume_whatever_the_git_label() -> None:
    relabelled = replace(CURRENT, git_revision="def", tree_state="unknown")

    assert _refusal(relabelled.as_dict()) is None


def test_a_receipt_from_before_digest_schemes_is_refused_with_its_reason() -> None:
    """Even when its digests happen to equal today's, the old formula could not see a library upgrade."""
    legacy = {key: value for key, value in CURRENT.as_dict().items() if key != "digest_scheme"}

    assert _refusal(legacy) == (
        "the search was launched before this service recorded its installed library versions, "
        "so a library change since launch cannot be ruled out; launch a fresh search"
    )


def test_moved_code_and_a_moved_environment_are_refused_for_what_moved() -> None:
    assert _refusal(replace(CURRENT, source_digest="0" * 64).as_dict()) == "the engine or strategy code changed since launch; launch a fresh search"
    assert _refusal(replace(CURRENT, environment_digest="0" * 64).as_dict()) == (
        "the Python interpreter or an installed library changed since launch; launch a fresh search"
    )


def test_the_legacy_reason_is_said_only_for_the_legacy_scheme() -> None:
    """A scheme this build never wrote — a future scheme read after a rollback,
    say — cannot honestly be told it predates recorded library versions (#2604)."""
    assert LEGACY_DIGEST_SCHEME < DIGEST_SCHEME  # the one scheme the legacy sentence fits

    future = _refusal(replace(CURRENT, digest_scheme=DIGEST_SCHEME + 1).as_dict())

    assert future == (
        "the search was recorded under a different identity scheme than this service uses, "
        "so its environment cannot be compared with this one; launch a fresh search"
    )
    assert "before this service recorded" not in future


def test_an_unreadable_environment_refuses_finish_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """The study detail view reads this refusal on every load (#2604).

    An ``EnvironmentIdentityError`` that escaped here 500ed the whole page and
    hid the recorded cells with it. The refusal is plain words for the owner;
    the underlying cause is logged, not shown.
    """

    def unreadable() -> CodeIdentity:
        raise EnvironmentIdentityError("the installed distribution 'x' has no name in its metadata")

    monkeypatch.setattr(lifecycle, "resolve_code_identity", unreadable)

    refusal = resume_refusal(
        _Interrupted(receipt={"code_identity": CURRENT.as_dict()}), noun="search", unit="cell", live=False
    )

    assert refusal == (
        "this service cannot identify its installed Python libraries right now, so it cannot prove "
        "the search would finish in the environment it launched under; repair the environment, then try again"
    )
