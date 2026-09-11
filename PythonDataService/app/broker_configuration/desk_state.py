"""Pure Alpaca desk projection over durable broker-configuration records.

This module authors every normal account-selection label, consequence and CTA
the desk renders. It deliberately has no credential resolver, broker client or
store: an effective selection is durable configuration state, never proof that
Alpaca is reachable or that a worker is currently alive.

Choosing an account on the desk is navigation only. Stage and Apply remain on
the Configuration page because staging advances the installation generation
and can consume a pending Apply.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.broker_configuration.records import (
    AccountNickname,
    AlpacaDeskState,
    BrokerProfile,
    DeskAccountChoice,
    DeskAction,
    DeskActionKind,
    DeskActivationState,
    DeskLifecycleStatus,
    DeskLifecycleStep,
    DeskSelectionSummary,
    InstallationSelection,
    ProfileRevision,
)


def _same_selection(
    left_profile_id: str,
    left_revision: int,
    right_profile_id: str | None,
    right_revision: int | None,
) -> bool:
    return left_profile_id == right_profile_id and left_revision == right_revision


def _selection_summary(
    *,
    profile: BrokerProfile,
    revision: ProfileRevision,
    account_id: str | None,
    nicknames: Mapping[str, str],
) -> DeskSelectionSummary:
    nickname = None if account_id is None else nicknames.get(account_id)
    mode_label = "Paper" if revision.endpoint_mode == "paper" else "Live"
    return DeskSelectionSummary(
        selection_id=f"{profile.profile_id}@{revision.revision}",
        profile_id=profile.profile_id,
        revision=revision.revision,
        profile_label=profile.display_name,
        account_id=account_id,
        nickname=nickname,
        account_label=nickname or profile.display_name,
        endpoint_mode=revision.endpoint_mode,
        badge_label=f"{mode_label} account",
        description=(
            f"{profile.display_name} uses the verified live account. Selecting this profile "
            "does not arm live trading."
            if account_id is not None and revision.endpoint_mode == "live"
            else (
                f"{profile.display_name} uses the verified paper account."
                if account_id is not None
                else f"{profile.display_name} still needs a verified account."
            )
        ),
    )


def _choice(
    *,
    summary: DeskSelectionSummary,
    is_staged: bool,
    is_effective: bool,
    apply_requested: bool,
) -> DeskAccountChoice:
    action_kind: DeskActionKind
    if is_staged and apply_requested:
        action_kind = "view_restart_steps"
        action_label = "View restart steps"
    elif is_staged and not is_effective:
        action_kind = "review_staged_configuration"
        action_label = f"Review & apply {summary.profile_label}"
    else:
        action_kind = "review_configuration"
        action_label = f"Review {summary.profile_label}"
    return DeskAccountChoice(
        selection_id=summary.selection_id,
        profile_id=summary.profile_id,
        revision=summary.revision,
        profile_label=summary.profile_label,
        account_id=summary.account_id,
        nickname=summary.nickname,
        account_label=summary.account_label,
        endpoint_mode=summary.endpoint_mode,
        badge_label=summary.badge_label,
        description=summary.description,
        action_kind=action_kind,
        action_label=action_label,
        is_staged=is_staged,
        is_effective=is_effective,
    )


def _activation_state(selection: InstallationSelection) -> DeskActivationState:
    if selection.apply_requested:
        return "apply_requested_restart_required"
    if (
        selection.staged_profile_id is not None
        and selection.staged_revision is not None
        and not _same_selection(
            selection.staged_profile_id,
            selection.staged_revision,
            selection.effective_profile_id,
            selection.effective_revision,
        )
    ):
        return "staged_not_applied"
    if selection.effective_profile_id is not None and selection.effective_revision is not None:
        return "effective_selection"
    return "no_selection"


def _lifecycle(
    activation_state: DeskActivationState, *, has_effective: bool
) -> tuple[DeskLifecycleStep, ...]:
    statuses: tuple[DeskLifecycleStatus, DeskLifecycleStatus, DeskLifecycleStatus]
    if activation_state == "no_selection":
        statuses = ("current", "pending", "pending")
    elif activation_state == "staged_not_applied":
        statuses = ("complete" if has_effective else "pending", "current", "pending")
    elif activation_state == "apply_requested_restart_required":
        statuses = ("complete" if has_effective else "pending", "complete", "current")
    else:
        statuses = ("complete", "complete", "complete")
    return tuple(
        DeskLifecycleStep(
            key=key,
            label=label,
            status=status,
            status_label={
                "complete": "Complete",
                "current": "Current step",
                "pending": "Pending",
            }[status],
        )
        for key, label, status in zip(
            ("effective_configuration", "selected_configuration", "worker_handoff"),
            ("Effective configuration", "Selected configuration", "Worker handoff"),
            statuses,
            strict=True,
        )
    )


def _setup_message(count: int) -> str | None:
    if count == 0:
        return None
    noun = "profile" if count == 1 else "profiles"
    pronoun = "it" if count == 1 else "they"
    verb = "needs" if count == 1 else "need"
    return (
        f"{count} saved {noun} still {verb} configuration or account verification before "
        f"{pronoun} can be selected."
    )


def _selection_reference(
    *,
    profile_id: str | None,
    revision: ProfileRevision | None,
    account_id: str | None,
    profiles: Mapping[str, BrokerProfile],
    nicknames: Mapping[str, str],
) -> DeskSelectionSummary | None:
    if profile_id is None or revision is None:
        return None
    profile = profiles[profile_id]
    return _selection_summary(
        profile=profile,
        revision=revision,
        account_id=account_id if account_id is not None else revision.account_pin,
        nicknames=nicknames,
    )


def project_desk_state(
    *,
    selection: InstallationSelection,
    profiles: Sequence[BrokerProfile],
    revisions_by_profile: Mapping[str, Sequence[ProfileRevision]],
    staged_revision: ProfileRevision | None,
    effective_revision: ProfileRevision | None,
    nicknames: Sequence[AccountNickname],
) -> AlpacaDeskState:
    """Build the desk read model without probing credentials or Alpaca."""
    profile_by_id = {profile.profile_id: profile for profile in profiles}
    nickname_by_account = {
        nickname.account_id: nickname.nickname for nickname in nicknames
    }
    choices: list[DeskAccountChoice] = []
    profiles_with_choices: set[str] = set()
    for profile in profiles:
        for revision in reversed(revisions_by_profile[profile.profile_id]):
            if not revision.complete or revision.account_pin is None:
                continue
            profiles_with_choices.add(profile.profile_id)
            summary = _selection_summary(
                profile=profile,
                revision=revision,
                account_id=revision.account_pin,
                nicknames=nickname_by_account,
            )
            choices.append(
                _choice(
                    summary=summary,
                    is_staged=_same_selection(
                        profile.profile_id,
                        revision.revision,
                        selection.staged_profile_id,
                        selection.staged_revision,
                    ),
                    is_effective=_same_selection(
                        profile.profile_id,
                        revision.revision,
                        selection.effective_profile_id,
                        selection.effective_revision,
                    ),
                    apply_requested=selection.apply_requested,
                )
            )

    staged = _selection_reference(
        profile_id=selection.staged_profile_id,
        revision=staged_revision,
        account_id=None,
        profiles=profile_by_id,
        nicknames=nickname_by_account,
    )
    effective = _selection_reference(
        profile_id=selection.effective_profile_id,
        revision=effective_revision,
        account_id=selection.effective_account_id,
        profiles=profile_by_id,
        nicknames=nickname_by_account,
    )
    activation_state = _activation_state(selection)
    profiles_requiring_setup = len(profiles) - len(profiles_with_choices)

    if activation_state == "no_selection":
        headline = "No Alpaca account is active for this installation"
        detail = "Choose a verified account configuration for this worker."
        selection_label = "Choose an account configuration"
        consequence = (
            "Choosing here only opens the saved configuration for review. Stage and Apply "
            "remain explicit actions on the Configuration page."
        )
        action = DeskAction(
            kind="review_configuration",
            label="Choose an account" if choices else "Set up an account",
            enabled=True,
        )
    elif activation_state == "staged_not_applied":
        if staged is None:  # pragma: no cover - invalid input record set
            raise ValueError("staged_not_applied requires a staged selection")
        headline = (
            f"{effective.account_label} remains the effective selection"
            if effective is not None
            else f"{staged.account_label} is selected, but not effective for this installation"
        )
        detail = (
            f"{staged.profile_label} is selected next; the effective configuration remains "
            f"{effective.profile_label}."
            if effective is not None
            else f"{staged.profile_label} is staged. Review it before recording Apply."
        )
        selection_label = f"Selected: {staged.account_label}"
        consequence = (
            "Apply records the change for the next controlled worker restart. It does not "
            "switch a running worker or arm live trading."
        )
        action = DeskAction(
            kind="review_staged_configuration",
            label=f"Review & apply {staged.profile_label}",
            enabled=True,
        )
    elif activation_state == "apply_requested_restart_required":
        if staged is None:  # pragma: no cover - invalid input record set
            raise ValueError("apply_requested_restart_required requires a staged selection")
        headline = f"{staged.account_label} is ready for a controlled restart"
        detail = (
            "Apply is recorded. A controlled restart of the worker is required before the "
            "selected configuration becomes effective."
        )
        selection_label = f"Apply recorded: {staged.account_label}"
        consequence = (
            "Restarting applies this exact profile revision. It does not arm live trading or "
            "retarget any existing strategy."
        )
        action = DeskAction(
            kind="view_restart_steps",
            label="View restart steps",
            enabled=True,
        )
    else:
        if effective is None:  # pragma: no cover - invalid input record set
            raise ValueError("effective_selection requires an effective selection")
        headline = f"{effective.account_label} is the effective selection for this installation"
        detail = f"The worker last acknowledged {effective.profile_label}."
        selection_label = f"Effective: {effective.account_label}"
        consequence = (
            "This is durable configuration state, not a connectivity claim. Current broker "
            "reachability is reported separately."
        )
        action = DeskAction(
            kind="review_configuration",
            label="Review account configuration",
            enabled=True,
        )

    return AlpacaDeskState(
        activation_state=activation_state,
        headline=headline,
        detail=detail,
        lifecycle=_lifecycle(activation_state, has_effective=effective is not None),
        selection_label=selection_label,
        consequence=consequence,
        action=action,
        selection_generation=selection.selection_generation,
        staged_choice=staged,
        effective_choice=effective,
        choices=tuple(choices),
        empty_choices_message=(
            None
            if choices
            else (
                "No verified accounts are ready to choose. Finish account verification on "
                "the Configuration page."
                if profiles_requiring_setup
                else "No account configurations are saved yet. Set one up to continue."
            )
        ),
        profiles_requiring_setup=profiles_requiring_setup,
        setup_required_message=_setup_message(profiles_requiring_setup),
    )


__all__ = ["project_desk_state"]
