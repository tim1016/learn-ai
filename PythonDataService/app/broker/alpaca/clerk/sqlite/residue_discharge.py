"""Operator discharge of a stranded attributed residue (#2381).

A strategy can hold an attributed residue the broker does not: a reducing
fill the Clerk never attributed (#2305's class) leaves ``EXIT_NOT_FLAT`` open,
the watchdog correctly refuses to sell into a flat broker (#2343), and the
episode escalates to ``EXIT_STUCK``. Both end only on attributed-flat proof,
and lane quiet refuses while either is open (#2344), so without this step the
only exit was force-retire, which strands the account for good.

The discharge is the operator's acknowledgement that the residue is not real,
but the Clerk does not take the operator's word for the broker. It re-reads
the account and writes the residue to zero only when that read proves the
discharge *restores* agreement: the broker's signed position for the symbol
equals the account-wide attribution minus the residue, and nothing — at the
broker or in the Clerk — is working on the symbol. That is the flat broker of
the issue, and it also covers a netted account (A's residue +10 beside B's
real -10 against a broker at -10). The zeroing is one hash-chained,
operator-confirmed ``ATTRIBUTED_RESIDUE_DISCHARGED`` transition (the typed
confirmation is the acknowledgement; the operator's reason is recorded when
given), after which the strategy's EXIT fences resolve through the existing
attributed-flat proof.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.exit_watchdog import (
    BrokerSymbolReader,
    clerk_work_in_flight,
)
from app.broker.alpaca.clerk.sqlite.facts import AttributedResidueDischargedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.reconcile import (
    broker_symbol_reader,
    read_account_open_work,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import resolve_flat_exit_fences
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import residue_discharge_role
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

ATTRIBUTED_RESIDUE_DISCHARGED = "ATTRIBUTED_RESIDUE_DISCHARGED"


class ResidueDischargeRefused(Exception):
    """The Clerk refused the discharge before writing anything."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ResidueDischargeReceipt:
    sequence: int
    recorded_at_ms: int
    symbol: str
    discharged_qty: float


async def discharge_attributed_residue(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    intake: ReentrantAsyncLock,
    strategy_instance_id: str,
    symbol: str,
    operator_reason: str | None,
) -> ResidueDischargeReceipt:
    """Zero one strategy's stranded residue in ``symbol`` once the broker proves it.

    The broker is read outside the intake fence, then every check and the
    append run in one fenced fold, so the attribution checked is the
    attribution zeroed. A ``BrokerError`` propagates: an unreadable broker is
    no proof.
    """
    broker_orders, broker_positions = await read_account_open_work(read)
    broker_observed_at_ms = repo.clock()
    return await intake.off_loop(
        _discharge,
        repo,
        broker_symbol=broker_symbol_reader(
            repo, broker_orders=broker_orders, broker_positions=broker_positions
        ),
        broker_observed_at_ms=broker_observed_at_ms,
        strategy_instance_id=strategy_instance_id,
        symbol=symbol.upper(),
        operator_reason=(operator_reason or "").strip() or None,
    )


def _discharge(
    repo: ClerkSqliteRepository,
    *,
    broker_symbol: BrokerSymbolReader,
    broker_observed_at_ms: int,
    strategy_instance_id: str,
    symbol: str,
    operator_reason: str | None,
) -> ResidueDischargeReceipt:
    residue = repo.position(strategy_instance_id, symbol)
    if not position_quantity_is_nonzero(residue):
        raise ResidueDischargeRefused(
            "NO_ATTRIBUTED_RESIDUE",
            f"The Clerk attributes no {symbol} to this bot; there is nothing to discharge.",
        )
    in_scope = [
        episode
        for episode in repo.active_uncertainties()
        if episode["scope"] == "ACCOUNT_CLERK"
        or episode["strategy_instance_id"] == strategy_instance_id
    ]
    refusing = sorted(
        {
            episode["reason_code"]
            for episode in in_scope
            if residue_discharge_role(episode["reason_code"]) == "refuses"
        }
    )
    if refusing:
        raise ResidueDischargeRefused(
            "UNCERTAINTY_REFUSES_DISCHARGE",
            f"Open {', '.join(refusing)} may mean the Clerk has not yet recorded a "
            "fill that moves this residue; resolve it first.",
        )
    episodes = [
        episode
        for episode in in_scope
        if residue_discharge_role(episode["reason_code"]) == "strands"
    ]
    if not episodes:
        raise ResidueDischargeRefused(
            "NO_STRANDED_EXIT_EPISODE",
            "No open EXIT_NOT_FLAT or EXIT_STUCK episode names this residue; "
            "flatten real exposure instead of discharging it.",
        )
    if repo.active_run(strategy_instance_id) is not None:
        raise ResidueDischargeRefused(
            "RUN_STILL_ACTIVE",
            "Stop the bot's active run before discharging its residue.",
        )
    if clerk_work_in_flight(repo, symbol):
        raise ResidueDischargeRefused(
            "CLERK_WORK_IN_FLIGHT",
            f"The Clerk still has {symbol} work in flight that could fill; "
            "let it finish, then retry.",
        )
    view = broker_symbol(symbol)
    if view.working:
        raise ResidueDischargeRefused(
            "BROKER_ORDER_WORKING",
            f"An order for {symbol} is working at the broker; cancel it or let it "
            "finish, then retry.",
        )
    if position_quantity_is_nonzero(view.broker_qty - (view.attributed_qty - residue)):
        raise ResidueDischargeRefused(
            "BROKER_DISAGREES_WITH_DISCHARGE",
            f"The broker holds {view.broker_qty:g} {symbol}; discharging this bot's "
            f"{residue:g} would leave the Clerk attributing "
            f"{view.attributed_qty - residue:g}. Only a residue the broker does not "
            "hold can be discharged.",
        )
    facts = AttributedResidueDischargedFacts(
        symbol=symbol,
        discharged_qty=residue,
        broker_qty=view.broker_qty,
        account_attributed_qty=view.attributed_qty,
        broker_observed_at_ms=broker_observed_at_ms,
        uncertainty_ids=sorted(episode["uncertainty_id"] for episode in episodes),
        operator_reason=operator_reason,
    )
    committed = repo.append_transition(
        TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind=ATTRIBUTED_RESIDUE_DISCHARGED,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=ATTRIBUTED_RESIDUE_DISCHARGED,
            facts_json=facts.to_facts_json(),
        )
    )
    logger.warning(
        "discharged a stranded attributed residue on operator acknowledgement",
        extra={
            "action": "attributed_residue_discharged",
            "account_id": repo.account_id,
            "strategy_instance_id": strategy_instance_id,
            "symbol": symbol,
            "discharged_qty": residue,
            "broker_qty": view.broker_qty,
            "sequence": committed.sequence,
        },
    )
    # Not in the append's transaction: a crash here leaves the residue zeroed
    # with the fences open, and the next clean reconciliation pass clears them
    # through this same attributed-flat proof.
    resolve_flat_exit_fences(
        repo,
        strategy_instance_id=strategy_instance_id,
        evidence_refs=(f"residue_discharge:{committed.sequence}", "attributed_flat"),
    )
    return ResidueDischargeReceipt(
        sequence=committed.sequence,
        recorded_at_ms=repo.clock(),
        symbol=symbol,
        discharged_qty=residue,
    )
