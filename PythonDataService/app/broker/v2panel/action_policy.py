"""Clear's ``archive``: the one rule, and the one action it presents (ADR 0052).

``evaluate_archive`` is the one definition of "may this registration be
archived", answered twice: by :func:`archive_action` against the projected
custody a bot's page shows, and by :mod:`app.services.bot_runner` against
freshly reconciled custody when the command commits. ``archive_action`` is
the page's presentation of that answer -- enablement, blockers, confirmation
and concurrency token -- and ``sqlite_panel_adapter`` presents it directly.

This module held the generic panel action registry until ``archive`` was the
only action left in it (#2635); a bot's other commands are the SQLite
Clerk's recovery catalog (``recovery_policy``). Copy stays in
``vocabulary.py`` (``copy_for``); execution stays in
``panel_data_source._action_performers``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.broker.v2panel.vocabulary import copy_for
from app.schemas.broker_v2_panel import PanelAction
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    ConditionScope,
    OperatorBlocker,
    OperatorConfirmationCopy,
)


def _blocker(
    condition_id: str,
    *,
    scope: ConditionScope,
    headline: str,
    detail: str,
    evidence: dict[str, str | int | float | bool | None] | None = None,
) -> OperatorBlocker:
    return OperatorBlocker.for_host(
        condition_id=condition_id,
        scope=scope,
        host="bot_cockpit",
        anchor=SURFACE_ANCHOR,
        disposition="wait",
        headline=headline,
        detail=detail,
        applies_to="run",
        evidence=evidence,
    )


ArchiveBlockedCause = Literal[
    "BOT_STILL_RUNNING",
    "ARCHIVE_SEALED_ACCOUNT_CUSTODY",
    "BOT_DUTY_NOT_SETTLED",
    "ARCHIVE_REHEARSAL_RECORDS_UNAVAILABLE",
    "ARCHIVE_REHEARSAL_STILL_HOLDS",
    "ARCHIVE_CUSTODY_UNPROVABLE",
    "ARCHIVE_WOULD_STRAND_CUSTODY",
]


@dataclass(frozen=True)
class RehearsalRecords:
    """What a graduated Shadow store's own records say a bot sealed on it holds (#2694).

    ``readable=False`` is a store that could not be opened, leased or read:
    it proves nothing. ``held`` names what the records show the bot still
    holds, in words a refusal can say; ``None`` is the proof that it holds
    nothing.
    """

    readable: bool
    held: str | None = None


@dataclass(frozen=True)
class ArchiveVerdict:
    """One definition of "may this registration be archived".

    Shared by :func:`archive_action`, which answers it against a projected
    custody snapshot to decide what to present, and by the committing operation in
    :mod:`app.services.bot_runner`, which answers it again against a freshly
    reconciled one before it writes. Archiving is irreversible and the
    presented decision is always older than the click, so the rule must hold
    at both moments and must be one rule, or the two drift.
    """

    eligible: bool
    cause: ArchiveBlockedCause | None = None
    already_retired: bool = False
    #: What ``ARCHIVE_REHEARSAL_STILL_HOLDS`` names: the holdings its records show.
    held: str | None = None


def evaluate_archive(
    *,
    running: bool,
    phase: str,
    custody_account_foreign: bool,
    has_exposure: bool,
    working_order_count: int,
    outstanding_effect_count: int,
    custody_provable: bool,
    rehearsal: RehearsalRecords | None = None,
) -> ArchiveVerdict:
    """Decide archive eligibility, nearest obstacle first (ADR 0052).

    Archive is the one exit for a registration the operator is *finished
    with* -- Clear on Home's Finished fold (#2567, #2578). Its enabling proof
    is custody: the registration is stopped, flat, and has no working orders.
    That is why ``custody_provable`` is checked *before* the exposure guard. A
    frozen account cannot prove exposure at all, so under a freeze
    ``has_exposure=False`` reports the Clerk's ignorance rather than the bot's
    flatness -- and archiving on it would be treating an unproven fact as an
    enabling one. The custody guard *is* the proof, so it must be believable
    before it is believed.

    ``running`` and ``phase`` are two different facts and both are required.
    ``running`` is process liveness; ``phase`` is the durable duty record. They
    disagree in exactly the window that matters -- a task that has died before
    its stop transition committed reads ``running=False`` while the authority
    still holds an ACTIVE run -- and archiving there would stamp
    ``retired_at_ms`` on a registration whose run never ended. The fold that
    writes it states there is no active run; this is what makes that true.

    ``custody_account_foreign`` is a registration sealed on an account the
    installed Clerk does not custody (#2589). With no ``rehearsal`` nothing
    can prove it holds nothing, and no wait changes that, so it is refused
    before its duty settles: a not-yet-settled refusal would promise a clear
    that never comes.

    ``rehearsal`` is that proof for the one such account that keeps one: a
    live account's Shadow store after graduation (#2694). No broker stands
    behind it and nothing reconciles it again, so its own records are the last
    word, and they stand in for the installed custody facts below. Its duty is
    settled through the same store (#2589), so the not-yet-settled promise
    holds for it. Records that could not be read prove nothing; records that
    show a holding refuse and name it.

    ``outstanding_effect_count`` is bot-scoped and asymmetric by design: the
    commit-time caller reads it from a freshly reconciled custody snapshot,
    while the presentation cannot see it and passes zero. That asymmetry is
    the same one the whole action already has -- the presented decision is
    always older than the click -- and it fails in the safe direction: an
    accepted-but-not-yet-working effect can arm the button and will still be
    refused at commit, rather than committing and letting the effect create
    broker custody for a terminal registration.
    """
    if phase == "RETIRED":
        return ArchiveVerdict(eligible=False, already_retired=True)
    if running:
        return ArchiveVerdict(eligible=False, cause="BOT_STILL_RUNNING")
    if custody_account_foreign and rehearsal is None:
        return ArchiveVerdict(eligible=False, cause="ARCHIVE_SEALED_ACCOUNT_CUSTODY")
    if phase != "OFF_DUTY":
        return ArchiveVerdict(eligible=False, cause="BOT_DUTY_NOT_SETTLED")
    if rehearsal is not None:
        if not rehearsal.readable:
            return ArchiveVerdict(eligible=False, cause="ARCHIVE_REHEARSAL_RECORDS_UNAVAILABLE")
        if rehearsal.held is not None:
            return ArchiveVerdict(eligible=False, cause="ARCHIVE_REHEARSAL_STILL_HOLDS", held=rehearsal.held)
        return ArchiveVerdict(eligible=True)
    if not custody_provable:
        return ArchiveVerdict(eligible=False, cause="ARCHIVE_CUSTODY_UNPROVABLE")
    if has_exposure or working_order_count or outstanding_effect_count:
        return ArchiveVerdict(eligible=False, cause="ARCHIVE_WOULD_STRAND_CUSTODY")
    return ArchiveVerdict(eligible=True)


#: The two rehearsal refusals (#2694), worded once for the page's blocker and
#: the commit's refusal alike. ``{held}`` is the verdict's ``held``.
REHEARSAL_RECORDS_UNAVAILABLE_COPY = (
    "This bot's Shadow records could not be read.",
    "It rehearsed in the account's Shadow world, and a bot is cleared only "
    "when its records show it holds nothing. They could not be opened and "
    "read just now, so nothing was changed.",
)
REHEARSAL_STILL_HOLDS_COPY = (
    "This bot's Shadow records still show {held}.",
    "It rehearsed in the account's Shadow world, and a bot is cleared only "
    "when its records show it holds nothing. Nothing trades or settles in "
    "that world now that the account is live, so the bot stays in History.",
)

_ARCHIVE_BLOCKER_COPY: dict[ArchiveBlockedCause, tuple[str, str]] = {
    "BOT_STILL_RUNNING": (
        "Stop the bot before clearing it.",
        "A running bot still evaluates bars and can place orders.",
    ),
    "ARCHIVE_SEALED_ACCOUNT_CUSTODY": (
        "This bot's account is no longer managed here.",
        "It ran on another account, such as a live account's rehearsal before "
        "it went live. Clearing needs that account to show the bot holds "
        "nothing, and the Clerk here can no longer check it.",
    ),
    "BOT_DUTY_NOT_SETTLED": (
        "This bot's last run has not finished settling.",
        "It ended without a clean stop. The Clerk records it as ended "
        "shortly, usually within a minute; then you can clear the bot. A Dry "
        "Run's is recorded when the service next starts.",
    ),
    "ARCHIVE_REHEARSAL_RECORDS_UNAVAILABLE": REHEARSAL_RECORDS_UNAVAILABLE_COPY,
    "ARCHIVE_REHEARSAL_STILL_HOLDS": REHEARSAL_STILL_HOLDS_COPY,
    "ARCHIVE_CUSTODY_UNPROVABLE": (
        "This account cannot prove the bot is flat.",
        "A bot is cleared only on proof that it holds nothing. Choose Reconcile "
        "now once Alpaca can be read, then clear it.",
    ),
    "ARCHIVE_WOULD_STRAND_CUSTODY": (
        "This bot still holds shares or has a working order.",
        "Flatten it and let its working orders finish, then clear it.",
    ),
}


def archive_action(
    *,
    running: bool,
    phase: str,
    freeze_active: bool,
    exposure: Mapping[str, float],
    working_order_count: int,
    account_id: str,
    strategy_instance_id: str,
    revision: int,
    custody_account_foreign: bool = False,
    rehearsal: RehearsalRecords | None = None,
) -> PanelAction:
    """Present the shared archive rule on one bot's page (ADR 0052).

    ``exposure`` is the bot's attributed net exposure per symbol; any nonzero
    quantity blocks archive while the bot still holds a position.

    A page is built from the installed Clerk's custody record, which a bot
    sealed on another account does not have -- its page is not found (#2589).
    Only a Clear request presents one for such a bot, from the runner's own
    duty record (``custody_account_foreign``, ``rehearsal``; #2694).
    """
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())
    verdict = evaluate_archive(
        running=running,
        phase=phase,
        custody_account_foreign=custody_account_foreign,
        has_exposure=has_exposure,
        working_order_count=working_order_count,
        # The panel has no bot-scoped effect count; the commit does, and it is
        # what enforces this. See `evaluate_archive` on why that asymmetry is
        # safe here and is the action's existing contract, not a gap in it.
        outstanding_effect_count=0,
        custody_provable=not freeze_active,
        rehearsal=rehearsal,
    )
    copy = copy_for("archive")
    return PanelAction(
        action_id="archive",
        label=copy.label,
        explanation=copy.explanation,
        enabled=verdict.eligible,
        blockers=_archive_blockers(verdict, strategy_instance_id=strategy_instance_id),
        confirmation=(
            _archive_confirmation(
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
                working_order_count=working_order_count,
            )
            if verdict.eligible
            else None
        ),
        revision=revision,
        concurrency_token=_archive_concurrency_token(
            phase=phase,
            running=running,
            has_exposure=has_exposure,
            exposure=exposure,
            working_order_count=working_order_count,
            freeze_active=freeze_active,
        ),
    )


def _archive_blockers(verdict: ArchiveVerdict, *, strategy_instance_id: str) -> list[OperatorBlocker]:
    """The verdict as operator guidance: nothing when eligible, else its one cause."""
    if verdict.eligible:
        return []
    evidence: dict[str, str | int | float | bool | None] = {"strategy_instance_id": strategy_instance_id}
    if verdict.already_retired:
        return [
            _blocker(
                "BOT_ALREADY_RETIRED",
                scope="bot",
                headline="This bot is already cleared.",
                detail="A cleared bot is off Home already; its history is kept.",
                evidence=evidence,
            )
        ]
    assert verdict.cause is not None
    headline, detail = _ARCHIVE_BLOCKER_COPY[verdict.cause]
    return [
        _blocker(
            verdict.cause,
            scope="account" if verdict.cause == "ARCHIVE_CUSTODY_UNPROVABLE" else "bot",
            headline=headline.format(held=verdict.held),
            detail=detail,
            evidence=evidence,
        )
    ]


def _archive_confirmation(
    *,
    account_id: str,
    strategy_instance_id: str,
    working_order_count: int,
) -> OperatorConfirmationCopy:
    """State the custody the operator is archiving *on*, not just the bot's name.

    Archive's enabling proof is that this bot holds nothing, so the
    confirmation quotes that proof back: an operator who sees "no attributed
    exposure and 0 working orders" is confirming the fact the rule actually
    used, and a stale screen showing otherwise is exactly what the
    commit-time re-check refuses.
    """
    return OperatorConfirmationCopy(
        title="Archive this bot?",
        body=(
            f"This takes {strategy_instance_id} on account "
            f"{account_id} off the roster. It is stopped, with no "
            f"attributed exposure and {working_order_count} working "
            "orders."
        ),
        consequence=(
            "The registration can start no further runs and its id is "
            "never reused. Its history and receipts are kept. This cannot "
            "be undone."
        ),
        confirm_label="Archive bot",
        required_token="ARCHIVE",
    )


def _archive_concurrency_token(
    *,
    phase: str,
    running: bool,
    has_exposure: bool,
    exposure: Mapping[str, float],
    working_order_count: int,
    freeze_active: bool,
) -> str:
    """Archive's own compare-and-set domain.

    Only the state its rule reads can change the token, so unrelated panel
    changes cannot manufacture a 409. The payload is byte-for-byte the one the
    retired registry hashed (#2635), so a page left open across the upgrade
    still posts a token the server accepts.
    """
    payload = {
        "action_id": "archive",
        "inputs": (
            phase,
            running,
            has_exposure,
            tuple(sorted(exposure.items())),
            working_order_count,
            freeze_active,
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:32]
