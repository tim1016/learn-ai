"""Finish against a recorded code identity (``lifecycle.resume_refusal``; #2588).

An interrupted record resumes only under the code and environment it was
launched with. A receipt written before the environment digest read the
installed packages (``digest_scheme`` absent) cannot show that no library
changed since, so it is refused with that reason rather than resumed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.research.persistence.lifecycle import resume_refusal
from app.research.sweep.identity import DIGEST_SCHEME, CodeIdentity

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
