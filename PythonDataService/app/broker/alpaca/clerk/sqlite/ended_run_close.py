"""Close what an ended bot holds, once per exposure, under a Clerk-owned decision namespace.

Two steps of the account reconciliation pass close an ended bot's position on
its own authority, each under its own decision-id prefix: a Dry Run's
run-end close (``dry_run_close``, owner decision 2026-09-29) and the sale at a
bot's owner-scheduled end (``scheduled_end``, #2607). Both need the same two
things, which live here once:

* **What is owed** (:func:`bot_holdings`): every symbol the bot holds, with the
  owned entry its close is keyed on (``exit.newest_reducible_entry``). The
  close's decision id is derived from that entry, so a re-run of the pass
  drives the same EXIT and never sells twice, with no "close owed" record of
  its own to lose. A holding another EXIT already owns is left to that EXIT.
* **Driving one close** (:func:`drive_close`): accept the recovery EXIT under
  intake, with no broker I/O, then resolve it outside intake like any EXIT.
  A run that re-activated before the capture, a held operation claim or a
  policy block defers it; an accepted EXIT is re-driven by the next pass's
  operation recovery, and one never accepted is owed again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite.exit import (
    RecoveryRunActiveError,
    accept_recovery_exit,
    exit_effect_operation_id,
    newest_reducible_entry,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import ExitSubmission
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.ports import BrokerTradePort


@dataclass(frozen=True)
class OwedClose:
    """One ended bot's position that one Clerk step has to close."""

    strategy_instance_id: str
    symbol: str
    entry_order_ref: str
    decision_id: str


@dataclass(frozen=True)
class Holding:
    """One symbol an ended bot holds, and where its close under one prefix stands.

    ``owed``: nothing has closed it and no EXIT owns it. ``closing``: the
    close under this prefix was accepted (driven, folded or finished).
    ``other_exit``: another EXIT -- the bot's own, the operator's -- still owns it.
    """

    close: OwedClose
    state: Literal["owed", "closing", "other_exit"]


def bot_holdings(repo: ClerkSqliteRepository, strategy_instance_id: str, *, decision_prefix: str) -> list[Holding]:
    """Every symbol one bot holds with an owned entry, keyed for a close under ``decision_prefix``."""
    held = [
        symbol
        for symbol, quantity in repo.attributed_positions_for_strategy(strategy_instance_id).items()
        if position_quantity_is_nonzero(quantity)
    ]
    holdings: list[Holding] = []
    for symbol in held:
        target = newest_reducible_entry(repo, strategy_instance_id, symbol)
        if target is None:
            continue
        decision_id = f"{decision_prefix}{hashlib.sha256(target.order_ref.encode('utf-8')).hexdigest()[:16]}"
        close = OwedClose(strategy_instance_id, symbol, target.order_ref, decision_id)
        if repo.effect_operation(
            exit_effect_operation_id(strategy_instance_id=strategy_instance_id, decision_id=decision_id)
        ) is not None:
            holdings.append(Holding(close, "closing"))
        elif target.exit_owned:
            holdings.append(Holding(close, "other_exit"))
        else:
            holdings.append(Holding(close, "owed"))
    return holdings


def closes_owed_under(
    repo: ClerkSqliteRepository, strategy_instance_id: str, *, decision_prefix: str
) -> list[OwedClose]:
    """Every position one bot holds that no EXIT owns and no close under ``decision_prefix`` took yet."""
    return [
        holding.close
        for holding in bot_holdings(repo, strategy_instance_id, decision_prefix=decision_prefix)
        if holding.state == "owed"
    ]


class CloseDeferred(Enum):
    """Why a close was not accepted this pass; the next pass owes it again."""

    RUN_ACTIVE = "run_active"
    BUSY = "busy"


async def drive_close(
    repo: ClerkSqliteRepository,
    close: OwedClose,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    run: OffLoop,
    regular_session_only: bool = False,
) -> ExitSubmission | CloseDeferred:
    """Accept one close as a recovery EXIT under intake, then resolve it outside intake.

    ``regular_session_only`` is recorded on the EXIT's acceptance
    (``exit.accept_recovery_exit``).
    """
    try:
        accepted = await intake.off_loop(
            accept_recovery_exit,
            repo,
            account_id=repo.account_id,
            strategy_instance_id=close.strategy_instance_id,
            decision_id=close.decision_id,
            entry_order_ref=close.entry_order_ref,
            forbid_active_run=True,
            regular_session_only=regular_session_only,
        )
        return await resolve_accepted_exit(repo, accepted=accepted, trade=trade, pricing=pricing, off_loop=run)
    except RecoveryRunActiveError:
        return CloseDeferred.RUN_ACTIVE
    except (OperationClaimError, AdmissionBlockedError):
        return CloseDeferred.BUSY


__all__ = [
    "CloseDeferred",
    "Holding",
    "OwedClose",
    "bot_holdings",
    "closes_owed_under",
    "drive_close",
]
