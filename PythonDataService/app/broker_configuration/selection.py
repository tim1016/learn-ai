"""The four transitions of the installation selection, side by side.

Pure functions over the frozen ``InstallationSelection``: no store, no clock,
no collaborators. They live together because the rule they share is easy to
get wrong one at a time — a missing generation bump on *one* of them is enough
to let a caller re-arm an Apply that was just refused, or to let a second
worker overwrite the effective binding. Reading them as a group is how you see
that all four advance it.

The service owns the preconditions and the write; this module owns only what
the next record looks like.

`selection_generation` — monotonic, and the installation's only fence, since
ADR 0060 Decision 5 deliberately ships no worker identity:

| Transition | Advances the generation | Consumes a pending Apply |
|---|---|---|
| `staged` | yes | yes — the request named a revision no longer staged |
| `apply_requested` | yes | records one |
| `effective_acknowledged` | yes | yes |
| `apply_refused` | yes | yes (ADR 0060 Decision 4.3) |

Contract §2.6 names only the first two. The other two advance it for the
reason stated in ``records.py``: whoever still holds the pre-consumption
generation must not be able to act on it.
"""

from __future__ import annotations

from dataclasses import replace

from app.broker_configuration.records import InstallationSelection


def staged(
    current: InstallationSelection, *, profile_id: str, revision: int
) -> InstallationSelection:
    """The operator picked an exact revision. This governs nothing by itself."""
    return replace(
        current,
        staged_profile_id=profile_id,
        staged_revision=revision,
        apply_requested=False,
        apply_requested_at_ms=None,
        apply_requested_generation=None,
        selection_generation=current.selection_generation + 1,
    )


def apply_requested(current: InstallationSelection, *, at_ms: int) -> InstallationSelection:
    """Record the one-shot Apply against the generation it was made at."""
    return replace(
        current,
        apply_requested=True,
        apply_requested_at_ms=at_ms,
        apply_requested_generation=current.selection_generation,
        selection_generation=current.selection_generation + 1,
    )


def effective_acknowledged(
    current: InstallationSelection,
    *,
    profile_id: str,
    revision: int,
    account_id: str | None,
    at_ms: int,
) -> InstallationSelection:
    """The worker bound a revision, after construction succeeded under its lease."""
    return replace(
        current,
        apply_requested=False,
        apply_requested_at_ms=None,
        apply_requested_generation=None,
        selection_generation=current.selection_generation + 1,
        effective_profile_id=profile_id,
        effective_revision=revision,
        effective_account_id=account_id,
        effective_acknowledged_at_ms=at_ms,
        last_apply_outcome="applied",
        last_apply_refusal_reason=None,
    )


def apply_refused(current: InstallationSelection, *, reason: str) -> InstallationSelection:
    """A refused Apply consumes its request; the effective binding is untouched."""
    return replace(
        current,
        apply_requested=False,
        apply_requested_at_ms=None,
        apply_requested_generation=None,
        selection_generation=current.selection_generation + 1,
        last_apply_outcome="refused",
        last_apply_refusal_reason=reason,
    )


def is_recorded_apply(current: InstallationSelection, expected_generation: int) -> bool:
    """Whether an Apply naming ``expected_generation`` is already recorded.

    Contract §5: "a repeated apply against an already-recorded generation is a
    no-op success." A retry may name either the generation the request was
    recorded at or the one it produced, so both count.
    """
    return current.apply_requested and expected_generation in (
        current.selection_generation,
        current.apply_requested_generation,
    )


def reference(profile_id: str | None, revision: int | None) -> str | None:
    """How the audit log names a profile revision. ``None`` when unset."""
    if profile_id is None or revision is None:
        return None
    return f"{profile_id}@{revision}"


__all__ = [
    "apply_refused",
    "apply_requested",
    "effective_acknowledged",
    "is_recorded_apply",
    "reference",
    "staged",
]
