"""Which revision a start binds, and whether binding it is a switch.

These are the rules behind four of package D's "done when" clauses, and they
are worth reading together: staging never retargets a running bot, a crash with
a staged selection boots last-effective, same-account recovery works, and a
different-account switch is the only shape that has to prove anything.
"""

from __future__ import annotations

import pytest

from app.broker_configuration.binding_decision import (
    BindingCandidate,
    BindingIntent,
    NothingToBind,
    SwitchVerdict,
    decide,
    needs_acknowledgement,
    switch_verdict,
)
from app.broker_configuration.records import InstallationSelection


def _selection(**overrides: object) -> InstallationSelection:
    base: dict[str, object] = {
        "staged_profile_id": None,
        "staged_revision": None,
        "apply_requested": False,
        "apply_requested_at_ms": None,
        "apply_requested_generation": None,
        "selection_generation": 7,
        "effective_profile_id": None,
        "effective_revision": None,
        "effective_account_id": None,
        "effective_acknowledged_at_ms": None,
        "last_apply_outcome": None,
        "last_apply_refusal_reason": None,
    }
    base.update(overrides)
    return InstallationSelection(**base)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> BindingCandidate:
    base: dict[str, object] = {
        "profile_id": "prof_new",
        "revision": 2,
        "intent": BindingIntent.APPLY,
        "selection_generation": 7,
        "previous_profile_id": "prof_old",
        "previous_revision": 1,
        "previous_account_id": "PA000OLD",
    }
    base.update(overrides)
    return BindingCandidate(**base)  # type: ignore[arg-type]


# ---- which revision a start binds ------------------------------------------


def test_a_staged_revision_alone_never_becomes_the_one_bound() -> None:
    """Staging is not a queued restart (ADR 0060 Decision 5)."""
    chosen = decide(
        _selection(
            staged_profile_id="prof_staged",
            staged_revision=4,
            effective_profile_id="prof_effective",
            effective_revision=1,
            effective_account_id="PA000OLD",
        ),
        has_any_profile=True,
    )

    assert isinstance(chosen, BindingCandidate)
    assert (chosen.profile_id, chosen.revision) == ("prof_effective", 1)
    assert chosen.intent is BindingIntent.RECOVER


def test_a_recorded_apply_binds_the_staged_revision() -> None:
    chosen = decide(
        _selection(
            staged_profile_id="prof_staged",
            staged_revision=4,
            apply_requested=True,
            effective_profile_id="prof_effective",
            effective_revision=1,
        ),
        has_any_profile=True,
    )

    assert isinstance(chosen, BindingCandidate)
    assert (chosen.profile_id, chosen.revision) == ("prof_staged", 4)
    assert chosen.intent is BindingIntent.APPLY


def test_a_crash_with_a_staged_selection_boots_the_last_effective_revision() -> None:
    """The acceptance matrix's crash row: staged, never applied, then a crash."""
    chosen = decide(
        _selection(
            staged_profile_id="prof_staged",
            staged_revision=9,
            apply_requested=False,
            effective_profile_id="prof_effective",
            effective_revision=3,
            effective_account_id="PA000OLD",
        ),
        has_any_profile=True,
    )

    assert isinstance(chosen, BindingCandidate)
    assert (chosen.profile_id, chosen.revision) == ("prof_effective", 3)


def test_an_apply_naming_nothing_staged_falls_back_to_the_effective_revision() -> None:
    """Defensive: the Apply route refuses this, so a start must not trust it."""
    chosen = decide(
        _selection(
            apply_requested=True,
            effective_profile_id="prof_effective",
            effective_revision=2,
        ),
        has_any_profile=True,
    )

    assert isinstance(chosen, BindingCandidate)
    assert chosen.intent is BindingIntent.RECOVER


def test_a_configured_installation_with_nothing_effective_is_not_unconfigured() -> None:
    """Profiles exist but none is applied: the gate closes, no environment fallback."""
    chosen = decide(_selection(), has_any_profile=True)

    assert chosen == NothingToBind(installation_is_unconfigured=False, selection_generation=7)


def test_an_installation_with_no_profiles_at_all_is_unconfigured() -> None:
    """Every deployment before package F's import. The bootstrap path."""
    chosen = decide(_selection(), has_any_profile=False)

    assert chosen == NothingToBind(installation_is_unconfigured=True, selection_generation=7)


# ---- whether binding it is a switch ----------------------------------------


def test_a_first_binding_strands_nothing() -> None:
    verdict = switch_verdict(
        _candidate(previous_profile_id=None, previous_revision=None, previous_account_id=None),
        candidate_account_pin="PA000NEW",
    )

    assert verdict is SwitchVerdict.NO_PREVIOUS_BINDING
    assert not verdict.requires_prior_account_clear()


def test_a_previous_binding_whose_account_was_never_recorded_must_be_proven_clear() -> None:
    """A revision WAS effective; which account it held is simply not known.

    Reading a missing account as an absent one is how a single boot that
    recorded no account would silently disarm the preflight for every switch
    afterwards.
    """
    verdict = switch_verdict(
        _candidate(previous_account_id=None), candidate_account_pin="PA000NEW"
    )

    assert verdict is SwitchVerdict.PREVIOUS_ACCOUNT_UNPROVABLE
    assert verdict.requires_prior_account_clear()


def test_the_same_account_under_a_different_revision_is_not_a_switch() -> None:
    """Two profiles can point at one account; a changed risk value re-arms, not refuses."""
    verdict = switch_verdict(
        _candidate(previous_account_id="PA000SAME"), candidate_account_pin="PA000SAME"
    )

    assert verdict is SwitchVerdict.SAME_ACCOUNT
    assert not verdict.requires_prior_account_clear()


def test_a_different_pinned_account_must_prove_the_previous_one_is_clear() -> None:
    verdict = switch_verdict(
        _candidate(previous_account_id="PA000OLD"), candidate_account_pin="PA000NEW"
    )

    assert verdict is SwitchVerdict.DIFFERENT_ACCOUNT
    assert verdict.requires_prior_account_clear()


def test_an_unpinned_candidate_cannot_prove_sameness_and_is_treated_as_a_switch() -> None:
    """Fail closed: absence of proof is not proof of absence."""
    verdict = switch_verdict(_candidate(previous_account_id="PA000OLD"), candidate_account_pin=None)

    assert verdict is SwitchVerdict.ACCOUNT_UNPROVABLE
    assert verdict.requires_prior_account_clear()


def test_the_credential_slot_is_never_what_decides_a_switch() -> None:
    """Two profiles sharing one slot can still reach two different accounts.

    Encoded as a property of the signature rather than a scenario: the verdict
    is computed from account identity only, and there is nowhere to pass a slot.
    """
    from inspect import signature

    assert "slot" not in str(signature(switch_verdict))


# ---- when the binding is written back --------------------------------------


def test_a_consumed_apply_is_always_acknowledged() -> None:
    """Otherwise the one-shot request stays pending for the next restart."""
    candidate = _candidate(
        intent=BindingIntent.APPLY,
        profile_id="prof_x",
        revision=1,
        previous_profile_id="prof_x",
        previous_revision=1,
        previous_account_id="PA000SAME",
    )

    assert needs_acknowledgement(candidate, bound_account_id="PA000SAME")


def test_an_ordinary_restart_of_an_unchanged_binding_writes_nothing() -> None:
    """A reboot must not advance the generation and conflict a staged edit."""
    candidate = _candidate(
        intent=BindingIntent.RECOVER,
        profile_id="prof_x",
        revision=3,
        previous_profile_id="prof_x",
        previous_revision=3,
        previous_account_id="PA000SAME",
    )

    assert not needs_acknowledgement(candidate, bound_account_id="PA000SAME")


@pytest.mark.parametrize(
    ("revision", "bound_account"),
    [
        pytest.param(4, "PA000SAME", id="the effective revision moved"),
        pytest.param(3, "PA000OTHER", id="the account underneath it moved"),
    ],
)
def test_a_binding_that_actually_changed_is_acknowledged(
    revision: int, bound_account: str
) -> None:
    candidate = _candidate(
        intent=BindingIntent.RECOVER,
        profile_id="prof_x",
        revision=revision,
        previous_profile_id="prof_x",
        previous_revision=3,
        previous_account_id="PA000SAME",
    )

    assert needs_acknowledgement(candidate, bound_account_id=bound_account)
