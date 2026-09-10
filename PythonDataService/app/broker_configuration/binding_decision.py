"""Which revision the worker binds, and whether binding it is a switch.

Pure functions over the frozen ``InstallationSelection``: no store, no broker,
no clock. They live apart from the ceremony in ``worker_binding.py`` for the
same reason package B's four selection transitions live apart from its service
— the rules are short, they are the ones that must be right, and reading them
as a group is how you see that they agree.

Two questions, and the whole of ADR 0060 Decision 5 is in the difference
between them.

**Which revision?** A staged revision governs nothing. It becomes the one the
worker binds only when the operator recorded an Apply against it. Every other
start — a crash, a reboot, ``restart.sh``, compose's ``restart: always``, a
refused Apply — binds the last *effective* revision. Staging is not a queued
restart, and there is no path where an unattended restart quietly installs a
configuration nobody applied.

**Is it a switch?** Recovery and switching are different refusal shapes:
recovering the same account with exposure open must work, and moving to a
*different* account while the previous one still has obligations must refuse,
because that is the outcome the whole plan exists to prevent — a position at
one account with the worker now writing to another and nobody able to EXIT.

The discriminator is **account identity**, never profile identity and never the
credential slot: two profiles can name one slot, and two profiles can point at
one account. A binding that keeps the same account under a different revision
strands nothing, so it is not a switch; what a changed risk value does there is
invalidate the arming and require a re-arm (owner decision D3), which is the
arming ledger's job, not a reason to refuse the boot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.broker_configuration.records import InstallationSelection


class BindingIntent(StrEnum):
    """Why the worker is binding the revision it chose."""

    RECOVER = "recover"
    """No Apply is pending: bind the last-effective revision."""

    APPLY = "apply"
    """An Apply is recorded: bind the staged revision, once it survives preflight."""


class SwitchVerdict(StrEnum):
    """What binding this candidate would do to the previous account's custody."""

    NO_PREVIOUS_BINDING = "no_previous_binding"
    """Nothing was ever effective, so there is nothing to strand."""

    SAME_ACCOUNT = "same_account"
    """The candidate is pinned to the account already bound. Not a switch."""

    DIFFERENT_ACCOUNT = "different_account"
    """The candidate is pinned to a different account. The prior one must be clear."""

    ACCOUNT_UNPROVABLE = "account_unprovable"
    """The candidate names no pinned account, so sameness cannot be proven."""

    PREVIOUS_ACCOUNT_UNPROVABLE = "previous_account_unprovable"
    """A revision was effective, but which account it held is not recorded."""

    def requires_prior_account_clear(self) -> bool:
        """Whether the previous account must be proven clear before binding.

        The two ``UNPROVABLE`` verdicts are grouped with ``DIFFERENT_ACCOUNT``
        on purpose: the plan's rule is that a start which cannot *prove* the
        prior account is clear must refuse. An unpinned candidate cannot prove
        it is the same account; a previous binding whose account was never
        recorded cannot prove it either. Both are treated as a switch.

        The second one is deliberately fail-closed rather than convenient,
        because the alternative reading — "no recorded account means nothing to
        strand" — is only true when nothing was ever effective, and that case
        already has its own verdict. Reading a *missing* account as an *absent*
        one is how a single boot that recorded no account would silently disarm
        this check for every switch afterwards.
        """
        return self in (
            SwitchVerdict.DIFFERENT_ACCOUNT,
            SwitchVerdict.ACCOUNT_UNPROVABLE,
            SwitchVerdict.PREVIOUS_ACCOUNT_UNPROVABLE,
        )


@dataclass(frozen=True)
class BindingCandidate:
    """The revision the worker will try to bind, and what it would replace."""

    profile_id: str
    revision: int
    intent: BindingIntent
    selection_generation: int
    previous_profile_id: str | None
    previous_revision: int | None
    previous_account_id: str | None

    @property
    def is_apply(self) -> bool:
        return self.intent is BindingIntent.APPLY

    def replaces_a_different_revision(self) -> bool:
        """Whether binding this changes the recorded effective revision."""
        return (self.previous_profile_id, self.previous_revision) != (
            self.profile_id,
            self.revision,
        )


@dataclass(frozen=True)
class NothingToBind:
    """No revision can be bound, and whether that is because nothing is configured.

    ``installation_is_unconfigured`` separates two states a worker must not
    confuse. An installation with **no profiles at all** has never been
    configured — this is every deployment before package F's import runs — and
    the worker bootstraps from the process environment exactly as it always
    has. An installation that *has* profiles but no effective selection is
    configured and simply not applied yet: that is the contract's
    ``broker_unconfigured``, the gate stays closed, and there is no environment
    fallback, because falling back on a *configured* installation is how a
    worker ends up trading under settings nobody selected.
    """

    installation_is_unconfigured: bool
    selection_generation: int


def decide(
    selection: InstallationSelection, *, has_any_profile: bool
) -> BindingCandidate | NothingToBind:
    """Choose the revision this start binds.

    The staged revision is chosen **only** when an Apply is recorded against
    it; everything else binds the last-effective one.
    """
    if (
        selection.apply_requested
        and selection.staged_profile_id is not None
        and selection.staged_revision is not None
    ):
        return BindingCandidate(
            profile_id=selection.staged_profile_id,
            revision=selection.staged_revision,
            intent=BindingIntent.APPLY,
            selection_generation=selection.selection_generation,
            previous_profile_id=selection.effective_profile_id,
            previous_revision=selection.effective_revision,
            previous_account_id=selection.effective_account_id,
        )

    if selection.effective_profile_id is not None and selection.effective_revision is not None:
        return BindingCandidate(
            profile_id=selection.effective_profile_id,
            revision=selection.effective_revision,
            intent=BindingIntent.RECOVER,
            selection_generation=selection.selection_generation,
            previous_profile_id=selection.effective_profile_id,
            previous_revision=selection.effective_revision,
            previous_account_id=selection.effective_account_id,
        )

    return NothingToBind(
        installation_is_unconfigured=not has_any_profile,
        selection_generation=selection.selection_generation,
    )


def switch_verdict(
    candidate: BindingCandidate, *, candidate_account_pin: str | None
) -> SwitchVerdict:
    """Whether binding ``candidate`` would leave a different account behind."""
    if candidate.previous_profile_id is None:
        return SwitchVerdict.NO_PREVIOUS_BINDING
    if candidate.previous_account_id is None:
        # A revision *was* effective — the profile id says so — but which
        # account it held is not recorded. That is not "nothing to strand".
        return SwitchVerdict.PREVIOUS_ACCOUNT_UNPROVABLE
    if candidate_account_pin is None:
        return SwitchVerdict.ACCOUNT_UNPROVABLE
    if candidate_account_pin == candidate.previous_account_id:
        return SwitchVerdict.SAME_ACCOUNT
    return SwitchVerdict.DIFFERENT_ACCOUNT


def needs_acknowledgement(
    candidate: BindingCandidate, *, bound_account_id: str | None
) -> bool:
    """Whether this binding is worth writing back to the selection row.

    An acknowledgement advances ``selection_generation`` (package B's judgment
    call 4), which invalidates whatever generation a browser is holding. Doing
    that on every ordinary restart would make an unrelated reboot silently
    conflict the operator's staged edit, and it would rewrite
    ``effective_acknowledged_at_ms`` with a fact the contract explicitly says it
    does not carry — §2.6 calls it "a historical acknowledgement" that "does
    **not** prove the worker is running now".

    So the write happens when it means something: an Apply was consumed, the
    effective revision actually moved, or the account underneath it did.
    """
    return (
        candidate.is_apply
        or candidate.replaces_a_different_revision()
        or bound_account_id != candidate.previous_account_id
    )


__all__ = [
    "BindingCandidate",
    "BindingIntent",
    "NothingToBind",
    "SwitchVerdict",
    "decide",
    "needs_acknowledgement",
    "switch_verdict",
]
