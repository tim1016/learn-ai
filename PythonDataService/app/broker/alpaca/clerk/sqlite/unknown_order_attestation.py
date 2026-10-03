"""An operator settles an EXIT whose closing order's outcome is unknown (#2854).

A closing order whose submit answer was lost, whose re-send keeps being
refused, or which the broker answered under another client order id leaves
its EXIT ``unknown``: the Clerk keeps custody of the position and sells
nothing more for the bot. It cannot settle such an order by itself, because
no read of its own shows what became of it.

The operator can look at the account at Alpaca. This module takes what they
saw as an *attestation*, and does not take their word for it: the Clerk
reads the broker again itself and settles the order only when its own reads
agree. It never sends an order.

Attestation ``absent`` -- "Alpaca has no such order". The Clerk reads the
account twice, the second time after the first returned (the re-read rule of
``residue_discharge``, for the same reason: one round reads its lists with no
consistency fence between them). Each round is the order's exact lookup, the
open orders, then the positions, then every order of any state sent since
the closing order was created. The order is ended as not sent only when
every read answered, no list was full, no order carries its client order id,
no order is working on its symbol, and no order the Clerk did not place is
on its side of its symbol since it was created: such an order may be this
one under another identity. The broker's position must equal the
account-wide attribution, unless an open ``POSITION_DRIFT`` already names
the symbol; that episode is what then keeps the Clerk from selling shares
Alpaca does not hold. The EXIT fails releasably through ``EXIT_NOT_FLAT``
and the attributed quantity is never changed.

Attestation ``exists`` -- "Alpaca shows it, under this Alpaca order id". The
Clerk reads that order by its broker id and takes it up only when it is this
order: it carries exactly the client order id the Clerk sent, and the
symbol, side and quantity the order was created with. Then it is folded as
the answer of the order's own exact lookup would be: a working order is
followed, a fill is credited once, an order that ended unfilled fails the
EXIT through ``EXIT_NOT_FLAT``. A wrong id, another bot's order, or an order
under another client order id is refused. Taking up an order Alpaca booked
under another client order id is not done here.

Everything runs under the EXIT's exclusive operation claim, the one
``resolve_exit`` takes: a pass of the Clerk that meets it defers the EXIT,
so the order cannot be sent again while it is being settled. Every durable
check and every write happen in one fenced run, which reads the order again
as the catalog presented it: an order sent again, acknowledged or filled
since refuses, and nothing is written.

One ``ORDER_OUTCOME_ATTESTED`` transition records who attested what, and
what the Clerk read.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.account_open_work import (
    broker_order_in_flight,
    open_order_snapshot_is_full,
)
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    fold_exit_attested_absent,
    fold_exit_attested_order,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    OrderOutcomeAttestedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import CommittedTransition, TransitionInput
from app.broker.alpaca.clerk.sqlite.off_loop import run_drained, to_thread
from app.broker.alpaca.clerk.sqlite.order_evidence import trade_port_folds_simulated_evidence
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ProjectedUnknownExitOrder,
    SettlementAttestation,
)
from app.broker.alpaca.clerk.sqlite.reconcile import broker_symbol_reader
from app.broker.alpaca.clerk.sqlite.recovery_policy import unknown_order_lookup_settles_at_ms
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    POSITION_DRIFT_REASON_CODE,
    PositionDriftCause,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.utils.et_words import et_when_words

logger = logging.getLogger(__name__)

ORDER_OUTCOME_ATTESTED = "ORDER_OUTCOME_ATTESTED"

ORDERS_SINCE_MARGIN_MS = 60_000
"""How far before the closing order's creation the read of every order starts.

The Clerk records the order's creation by its own clock and Alpaca dates an
order by its own, so the read starts a minute early: an order Alpaca dated
just before the Clerk's record of creating it is still read.
"""


class UnknownOrderSettlementRefused(Exception):
    """The Clerk refused to settle the order, having written nothing."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class OrderOutcomeAttestation:
    """What the operator says Alpaca shows for the order: no such order, or the order under ``broker_order_id``."""

    kind: SettlementAttestation
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "exists" and not (self.broker_order_id or "").strip():
            raise ValueError("An attestation that Alpaca shows the order names its Alpaca order id")
        if self.kind == "absent" and self.broker_order_id is not None:
            raise ValueError("An attestation that Alpaca has no such order names no Alpaca order id")


@dataclass(frozen=True)
class UnknownOrderSettlementReceipt:
    """The durable record of one settled order: its ``ORDER_OUTCOME_ATTESTED`` transition."""

    sequence: int
    recorded_at_ms: int
    order_ref: str
    attestation: SettlementAttestation


@dataclass(frozen=True)
class AttestationFindings:
    """What the Clerk read at Alpaca for an attestation, before anything is settled.

    ``lines`` are the Clerk's own sentences. ``would_settle`` is whether an
    attestation made now would settle the order; when it would not,
    ``refusal_reason_code`` says why and ``lines`` is the refusal.
    """

    attestation: SettlementAttestation
    would_settle: bool
    refusal_reason_code: str | None
    lines: tuple[str, ...]
    observed_at_ms: int


@dataclass(frozen=True)
class _AccountRead:
    """One round of the account reads an ``absent`` attestation rests on."""

    open_orders: list[BrokerOrder]
    positions: list[BrokerPosition]
    orders_since: list[BrokerOrder]


@dataclass(frozen=True)
class _AbsenceProof:
    """What both rounds agreed on, and the attribution it was set against."""

    open_orders_read: int
    orders_since_read: int
    broker_qty: float
    account_attributed_qty: float

    @property
    def account_agrees(self) -> bool:
        return not position_quantity_is_nonzero(self.broker_qty - self.account_attributed_qty)


@dataclass(frozen=True)
class _Commit:
    """Who settles the order, and why: absent for a check, which writes nothing."""

    operator: str
    operator_reason: str | None


async def settle_unknown_exit_order(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    attestation: OrderOutcomeAttestation,
    operator: str,
    operator_reason: str | None,
) -> UnknownOrderSettlementReceipt:
    """Settle ``presented`` on the operator's attestation, once the Clerk's own reads agree.

    ``presented`` is the order as the catalog offered it; it is read again
    under the claim and again as the settlement commits, and must still be
    that order. ``operator`` is who attests, recorded with their reason.
    Raises :class:`UnknownOrderSettlementRefused`, having written nothing,
    whenever the Clerk's reads do not bear the attestation out.
    """
    if not operator or len(operator) > 64:
        raise UnknownOrderSettlementRefused(
            "OPERATOR_UNKNOWN",
            "A settled order records who settled it, and no operator identity was given. "
            "Nothing was changed.",
        )
    settled = await _attest(
        repo,
        read=read,
        trade=trade,
        intake=intake,
        strategy_instance_id=strategy_instance_id,
        presented=presented,
        attestation=attestation,
        commit=_Commit(operator=operator, operator_reason=(operator_reason or "").strip() or None),
    )
    assert isinstance(settled, UnknownOrderSettlementReceipt)
    return settled


async def check_unknown_exit_order(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    attestation: OrderOutcomeAttestation,
) -> AttestationFindings:
    """What the Clerk reads at Alpaca for ``attestation`` now, settling nothing.

    The same reads and the same rules as :func:`settle_unknown_exit_order`,
    under the same claim, so what an operator is shown before they confirm is
    what the settlement would rest on; the settlement reads it all again. A
    refusal is an answer here, not an error.
    """
    try:
        findings = await _attest(
            repo,
            read=read,
            trade=trade,
            intake=intake,
            strategy_instance_id=strategy_instance_id,
            presented=presented,
            attestation=attestation,
            commit=None,
        )
    except UnknownOrderSettlementRefused as refused:
        return AttestationFindings(
            attestation=attestation.kind,
            would_settle=False,
            refusal_reason_code=refused.reason_code,
            lines=(str(refused),),
            observed_at_ms=repo.clock(),
        )
    assert isinstance(findings, AttestationFindings)
    return findings


async def _attest(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    attestation: OrderOutcomeAttestation,
    commit: _Commit | None,
) -> UnknownOrderSettlementReceipt | AttestationFindings:
    """Read the broker under the EXIT's claim, then check -- and for a commit, settle -- in one fenced run."""
    if trade_port_folds_simulated_evidence(trade):
        raise UnknownOrderSettlementRefused(
            "NOT_A_BROKER_ACCOUNT",
            "This bot trades in a simulation, which has no account at Alpaca to look at. "
            "Nothing was changed.",
        )
    await to_thread(
        lambda: _require_presented_order(
            repo, strategy_instance_id=strategy_instance_id, presented=presented
        )
    )
    try:
        claim_token = repo.claim_before_broker_contact(presented.effect_operation_id).token
    except OperationClaimError as busy:
        raise _exit_is_busy() from busy
    broker = ClaimedBrokerIO(
        repo=repo,
        effect_operation_id=presented.effect_operation_id,
        claim_token=claim_token,
        trade=trade,
    )
    try:
        if attestation.kind == "exists":
            assert attestation.broker_order_id is not None
            return await intake.off_loop(
                _settle_exists,
                repo,
                strategy_instance_id=strategy_instance_id,
                presented=presented,
                observed=await _read_order(broker, attestation.broker_order_id.strip()),
                observed_at_ms=repo.clock(),
                commit=commit,
            )
        since_ms = presented.created_at_ms - ORDERS_SINCE_MARGIN_MS
        rounds = (
            await _read_account(broker, read, presented, since_ms=since_ms),
            await _read_account(broker, read, presented, since_ms=since_ms),
        )
        return await intake.off_loop(
            _settle_absent,
            repo,
            strategy_instance_id=strategy_instance_id,
            presented=presented,
            rounds=rounds,
            since_ms=since_ms,
            observed_at_ms=repo.clock(),
            commit=commit,
        )
    except OperationClaimError as lost:
        raise _exit_is_busy() from lost
    finally:
        await run_drained(
            to_thread,
            lambda: repo.release_operation_claim(
                effect_operation_id=presented.effect_operation_id, token=claim_token
            ),
        )


def _exit_is_busy() -> UnknownOrderSettlementRefused:
    return UnknownOrderSettlementRefused(
        "EXIT_BUSY",
        "The Clerk is working on this exit right now. Try again in a few seconds. "
        "Nothing was changed.",
    )


def _require_presented_order(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
) -> None:
    """Refuse unless the order is still the one the catalog presented, and its bot is still stopped.

    Read through the catalog's own read (``unknown_exit_orders``), so what is
    checked here is what was offered. An order the Clerk sent again since
    compares unequal: its latest send is part of it.
    """
    current = next(
        (
            order
            for order in repo.unknown_exit_orders(strategy_instance_id)
            if order.order_ref == presented.order_ref
        ),
        None,
    )
    if current is None:
        raise UnknownOrderSettlementRefused(
            "ORDER_NO_LONGER_UNKNOWN",
            "The Clerk no longer holds this order as one it cannot confirm: Alpaca acknowledged "
            "or filled it, or its exit ended. Refresh the page. Nothing was changed.",
        )
    if current != presented:
        raise UnknownOrderSettlementRefused(
            "ORDER_SENT_AGAIN",
            "The Clerk sent this order again, or recorded a new answer for it, after this action "
            "was presented. Refresh the page and look at the account at Alpaca again. Nothing "
            "was changed.",
        )
    if repo.active_run(strategy_instance_id) is not None:
        raise UnknownOrderSettlementRefused(
            "RUN_STILL_ACTIVE",
            "Stop the bot before settling its uncertain order. Nothing was changed.",
        )


async def _read_account(
    broker: ClaimedBrokerIO,
    read: BrokerReadPort,
    presented: ProjectedUnknownExitOrder,
    *,
    since_ms: int,
) -> _AccountRead:
    """One round: the exact lookup, the open orders, then the positions, then every order since ``since_ms``.

    In that sequence an order that arrives between two reads of a round is
    in a later one: among the open orders if it works, among the orders
    since if it has ended. Any read that fails refuses; so does the order's
    own exact lookup answering.
    """
    exact = await broker.observe_exact(presented.order_ref)
    if isinstance(exact, BrokerError):
        raise _broker_unreadable(exact)
    if exact is not None:
        raise _order_is_at_alpaca(exact)
    account = await broker.observe_open_orders_then_positions(read)
    if isinstance(account, BrokerError):
        raise _broker_unreadable(account)
    open_orders, positions = account
    if isinstance(positions, BrokerError):
        raise _broker_unreadable(positions)
    orders_since = await broker.observe_orders_since(read, after_ms=since_ms)
    if isinstance(orders_since, BrokerError):
        raise _broker_unreadable(orders_since)
    return _AccountRead(open_orders=open_orders, positions=positions, orders_since=orders_since)


def _broker_unreadable(error: BrokerError) -> UnknownOrderSettlementRefused:
    return UnknownOrderSettlementRefused(
        "BROKER_UNREADABLE",
        f"The Clerk could not read the account at Alpaca ({error}), so it cannot confirm what "
        "Alpaca shows. Nothing was changed.",
    )


def _order_is_at_alpaca(order: BrokerOrder) -> UnknownOrderSettlementRefused:
    return UnknownOrderSettlementRefused(
        "ORDER_EXISTS_AT_BROKER",
        f"Alpaca shows this order: Alpaca order id {order.order_id!r}, {order.status or 'no status'}. "
        "It is not absent. Choose \"Alpaca shows it\" and give that order id. Nothing was changed.",
    )


def _may_be(order: BrokerOrder, *, symbol: str, side: str | None = None) -> bool:
    """Whether a listed order may be in ``symbol`` -- and, when asked, on ``side``.

    An order whose symbol or side the broker left blank or unreadable is
    never assumed to be somewhere else: it may be the very order asked about.
    """
    if order.unreadable_fields:
        return True
    in_symbol = not order.symbol.strip() or order.symbol.upper() == symbol.upper()
    if side is None:
        return in_symbol
    return in_symbol and (not order.side.strip() or order.side.lower() == side.lower())


def _open_drift_names(repo: ClerkSqliteRepository, symbol: str) -> bool:
    """Whether an open ``POSITION_DRIFT`` episode already names ``symbol``."""
    episode = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=POSITION_DRIFT_REASON_CODE, strategy_instance_id=None
    )
    if episode is None:
        return False
    try:
        cause = PositionDriftCause.from_mapping(
            UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).cause_facts
        )
    except (TypeError, ValueError, KeyError):
        # An episode this build cannot read names no symbol it can rely on.
        return False
    return any(position.symbol == symbol.upper() for position in cause.positions)


def _prove_absent(
    repo: ClerkSqliteRepository,
    presented: ProjectedUnknownExitOrder,
    rounds: tuple[_AccountRead, _AccountRead],
) -> _AbsenceProof:
    """Refuse unless both rounds bear out that Alpaca has no such order."""
    symbol, side = presented.symbol.upper(), presented.side.lower()
    known_refs = repo.all_order_refs()
    for round_ in rounds:
        if open_order_snapshot_is_full(round_.open_orders) or open_order_snapshot_is_full(
            round_.orders_since
        ):
            raise UnknownOrderSettlementRefused(
                "ORDER_LIST_INCOMPLETE",
                "Alpaca returned as many orders as one read holds, so there may be more than the "
                "Clerk saw and it cannot confirm this order is absent. Nothing was changed.",
            )
        listed = [*round_.open_orders, *round_.orders_since]
        for order in listed:
            if order.client_order_id == presented.order_ref:
                raise _order_is_at_alpaca(order)
        for order in listed:
            if broker_order_in_flight(order) and _may_be(order, symbol=symbol):
                raise UnknownOrderSettlementRefused(
                    "BROKER_ORDER_WORKING",
                    f"An order for {symbol} is working at Alpaca (Alpaca order id {order.order_id!r}). "
                    "Cancel it or let it finish, then settle again. Nothing was changed.",
                )
        for order in listed:
            if not _may_be(order, symbol=symbol, side=side):
                continue
            # The Clerk's own orders are captured by their client order id, a
            # manual order Alpaca replaced by its broker order id (#2656).
            if (
                order.client_order_id not in known_refs
                and repo.manual_chain_order_ref(order.order_id) is None
            ):
                raise UnknownOrderSettlementRefused(
                    "UNKNOWN_ORDER_ON_SYMBOL",
                    f"Alpaca shows an order to {side} {symbol} that the Clerk did not place "
                    f"(Alpaca order id {order.order_id!r}), sent since this order was. It may be "
                    "this order under another id, so the Clerk cannot end this order as not sent. "
                    "Nothing was changed.",
                )
    earlier, later = (
        broker_symbol_reader(
            repo, broker_orders=round_.open_orders, broker_positions=round_.positions
        )(symbol)
        for round_ in rounds
    )
    if not all(
        math.isfinite(quantity)
        for quantity in (earlier.broker_qty, later.broker_qty, later.attributed_qty)
    ):
        raise UnknownOrderSettlementRefused(
            "EXPOSURE_NOT_PROVEN",
            f"A {symbol} quantity at Alpaca or in the Clerk's records is not a finite number. "
            "Nothing was changed.",
        )
    if position_quantity_is_nonzero(earlier.broker_qty - later.broker_qty):
        raise UnknownOrderSettlementRefused(
            "BROKER_SNAPSHOT_UNSETTLED",
            f"Alpaca's {symbol} position changed between the Clerk's two reads. Settle again once "
            "it is still. Nothing was changed.",
        )
    proof = _AbsenceProof(
        open_orders_read=len(rounds[1].open_orders),
        orders_since_read=len(rounds[1].orders_since),
        broker_qty=later.broker_qty,
        account_attributed_qty=later.attributed_qty,
    )
    if not proof.account_agrees and not _open_drift_names(repo, symbol):
        raise UnknownOrderSettlementRefused(
            "POSITION_DIFFERENCE_NOT_RECORDED",
            f"Alpaca holds {proof.broker_qty:g} {symbol} and the Clerk's records say "
            f"{proof.account_attributed_qty:g}. The Clerk has not recorded that difference yet. "
            "Check the account against Alpaca, then settle again. Nothing was changed.",
        )
    return proof


def _settle_absent(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    rounds: tuple[_AccountRead, _AccountRead],
    since_ms: int,
    observed_at_ms: int,
    commit: _Commit | None,
) -> UnknownOrderSettlementReceipt | AttestationFindings:
    """The fenced run of an ``absent`` attestation: every durable check, then -- for a commit -- the settlement."""
    _require_presented_order(repo, strategy_instance_id=strategy_instance_id, presented=presented)
    settles_at_ms = unknown_order_lookup_settles_at_ms(presented)
    now_ms = repo.clock()
    if now_ms < settles_at_ms:
        # An order sent moments ago may be at Alpaca and not yet show in a
        # read; the Clerk's own re-send waits out the same window.
        raise UnknownOrderSettlementRefused(
            "ORDER_LOOKUP_STILL_SETTLING",
            "The Clerk sent this order moments ago, and Alpaca may not show it yet. Settle again "
            f"after {et_when_words(settles_at_ms, now_ms=now_ms)}. Nothing was changed.",
        )
    proof = _prove_absent(repo, presented, rounds)
    if commit is None:
        return AttestationFindings(
            attestation="absent",
            would_settle=True,
            refusal_reason_code=None,
            lines=_absence_lines(presented, proof),
            observed_at_ms=observed_at_ms,
        )
    committed = _append_attestation(
        repo,
        presented=presented,
        facts=OrderOutcomeAttestedFacts(
            attestation="absent",
            operator=commit.operator,
            operator_reason=commit.operator_reason,
            client_order_id=presented.order_ref,
            symbol=presented.symbol,
            side=presented.side,
            quantity=presented.quantity,
            latest_send_sequence=presented.latest_send_sequence,
            uncertainty_id=presented.uncertainty_id,
            broker_observed_at_ms=observed_at_ms,
            open_orders_read=proof.open_orders_read,
            orders_since_read=proof.orders_since_read,
            orders_read_since_ms=since_ms,
            broker_qty=proof.broker_qty,
            account_attributed_qty=proof.account_attributed_qty,
        ),
        observed_at_ms=observed_at_ms,
    )
    fold_exit_attested_absent(
        repo,
        effect_operation_id=presented.effect_operation_id,
        order_ref=presented.order_ref,
        broker_qty=proof.broker_qty,
        account_attributed_qty=proof.account_attributed_qty,
    )
    return _settled(repo, committed, presented, commit, attestation="absent")


def _absence_lines(presented: ProjectedUnknownExitOrder, proof: _AbsenceProof) -> tuple[str, ...]:
    """What the Clerk found, as the operator reads it before confirming."""
    symbol, side = presented.symbol.upper(), presented.side.lower()
    position = (
        f"Alpaca holds {proof.broker_qty:g} {symbol}, as the Clerk's records say."
        if proof.account_agrees
        else (
            f"Alpaca holds {proof.broker_qty:g} {symbol} and the Clerk's records say "
            f"{proof.account_attributed_qty:g}. That difference is already recorded. After "
            "settling, write off the missing shares if Alpaca does not hold them."
        )
    )
    return (
        "Alpaca shows no order under this order's id.",
        f"No order is working on {symbol}, and Alpaca shows no other order to {side} {symbol} "
        "since this one was sent.",
        position,
    )


def _append_attestation(
    repo: ClerkSqliteRepository,
    *,
    presented: ProjectedUnknownExitOrder,
    facts: OrderOutcomeAttestedFacts,
    observed_at_ms: int,
) -> CommittedTransition:
    """Record who attested what, and what the Clerk read, on the EXIT's own timeline."""
    effect = repo.effect_operation(presented.effect_operation_id)
    assert effect is not None
    return repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            order_ref=presented.order_ref,
            transition_kind=ORDER_OUTCOME_ATTESTED,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=effect.state,
            clerk_observed_at_ms=observed_at_ms,
            summary_code=ORDER_OUTCOME_ATTESTED,
            facts_json=facts.to_facts_json(),
        )
    )


def _settled(
    repo: ClerkSqliteRepository,
    committed: CommittedTransition,
    presented: ProjectedUnknownExitOrder,
    commit: _Commit,
    *,
    attestation: SettlementAttestation,
) -> UnknownOrderSettlementReceipt:
    effect = repo.effect_operation(presented.effect_operation_id)
    assert effect is not None
    logger.warning(
        "an operator settled a closing order whose outcome was unknown",
        extra={
            "action": "unknown_exit_order_settled",
            "account_id": repo.account_id,
            "strategy_instance_id": effect.strategy_instance_id,
            "effect_operation_id": effect.effect_operation_id,
            "order_ref": presented.order_ref,
            "attestation": attestation,
            "operator": commit.operator,
            "exit_state": effect.state,
            "sequence": committed.sequence,
        },
    )
    return UnknownOrderSettlementReceipt(
        sequence=committed.sequence,
        recorded_at_ms=repo.clock(),
        order_ref=presented.order_ref,
        attestation=attestation,
    )


async def _read_order(broker: ClaimedBrokerIO, broker_order_id: str) -> BrokerOrder:
    """The order Alpaca has under ``broker_order_id``, read under the claim; refuses when it has none."""
    observed = await broker.observe_broker_order(broker_order_id)
    if isinstance(observed, BrokerError):
        raise _broker_unreadable(observed)
    if observed is None:
        raise UnknownOrderSettlementRefused(
            "NO_SUCH_BROKER_ORDER",
            f"Alpaca has no order with the id {broker_order_id!r}. Check the id at Alpaca. "
            "Nothing was changed.",
        )
    return observed


def _require_same_order(presented: ProjectedUnknownExitOrder, observed: BrokerOrder) -> None:
    """Refuse unless the order Alpaca returned is this closing order, readable in full.

    It is the same order only when it carries exactly the client order id
    this order was sent under, and the symbol, side and quantity it was
    created with. Another bot's order, an order placed at Alpaca by hand, and
    an order Alpaca booked under another client order id all fail the first
    test, whatever else matches.

    An order the Clerk could not read in full is refused too, before
    anything is written: its fold would otherwise withhold it and record
    only that the outcome is still unknown.
    """
    order_id = observed.order_id
    named = all(value.strip() for value in (order_id, observed.status, observed.symbol, observed.side))
    if observed.unreadable_fields or not named:
        raise UnknownOrderSettlementRefused(
            "ORDER_UNREADABLE",
            f"The Clerk could not read all of Alpaca order {order_id!r}, so it cannot tell whether "
            "it is this order. Nothing was changed.",
        )
    if observed.client_order_id != presented.order_ref:
        raise UnknownOrderSettlementRefused(
            "NOT_THIS_ORDER",
            f"Alpaca order {order_id!r} is not this order: Alpaca holds it under client order id "
            f"{observed.client_order_id!r}, and the Clerk sent this order under "
            f"{presented.order_ref!r}. Nothing was changed.",
        )
    quantity = observed.quantity
    if (
        observed.symbol.upper() != presented.symbol.upper()
        or observed.side.lower() != presented.side.lower()
        or quantity is None
        or not math.isfinite(quantity)
        or position_quantity_is_nonzero(quantity - presented.quantity)
    ):
        raise UnknownOrderSettlementRefused(
            "NOT_THIS_ORDER",
            f"Alpaca order {order_id!r} is to {observed.side.lower()} "
            f"{'an unstated number of' if quantity is None else format(quantity, 'g')} "
            f"{observed.symbol}, and this order is to {presented.side.lower()} "
            f"{presented.quantity:g} {presented.symbol}. Nothing was changed.",
        )
    filled, price = observed.filled_quantity, observed.filled_avg_price
    if (
        not math.isfinite(filled)
        or filled < 0
        or position_quantity_is_nonzero(max(filled - quantity, 0.0))
        or (position_quantity_is_nonzero(filled) and (price is None or not math.isfinite(price) or price <= 0))
    ):
        raise UnknownOrderSettlementRefused(
            "ORDER_UNREADABLE",
            f"Alpaca reports a filled quantity or price for order {order_id!r} that the Clerk "
            "cannot record. Nothing was changed.",
        )


def _settle_exists(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    observed: BrokerOrder,
    observed_at_ms: int,
    commit: _Commit | None,
) -> UnknownOrderSettlementReceipt | AttestationFindings:
    """The fenced run of an ``exists`` attestation: the order is this one, then -- for a commit -- it is taken up."""
    _require_presented_order(repo, strategy_instance_id=strategy_instance_id, presented=presented)
    _require_same_order(presented, observed)
    if commit is None:
        return AttestationFindings(
            attestation="exists",
            would_settle=True,
            refusal_reason_code=None,
            lines=(
                f"Alpaca order {observed.order_id}: {observed.side.lower()} {presented.quantity:g} "
                f"{presented.symbol.upper()}, {observed.status}, {observed.filled_quantity:g} filled.",
                "It carries this order's client order id, symbol, side and quantity.",
                "The Clerk takes it up as this exit's order. Nothing is sent.",
            ),
            observed_at_ms=observed_at_ms,
        )
    committed = _append_attestation(
        repo,
        presented=presented,
        facts=OrderOutcomeAttestedFacts(
            attestation="exists",
            operator=commit.operator,
            operator_reason=commit.operator_reason,
            client_order_id=presented.order_ref,
            symbol=presented.symbol,
            side=presented.side,
            quantity=presented.quantity,
            latest_send_sequence=presented.latest_send_sequence,
            uncertainty_id=presented.uncertainty_id,
            broker_observed_at_ms=observed_at_ms,
            broker_order_id=observed.order_id,
            broker_state=observed.status,
            broker_filled_quantity=observed.filled_quantity,
        ),
        observed_at_ms=observed_at_ms,
    )
    fold_exit_attested_order(
        repo, effect_operation_id=presented.effect_operation_id, observed=observed
    )
    return _settled(repo, committed, presented, commit, attestation="exists")


def recorded_settlement(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    order_ref: str,
    attestation: OrderOutcomeAttestation,
) -> UnknownOrderSettlementReceipt | None:
    """The receipt of this same settlement, when it was already made: a replay does nothing twice.

    ``None`` unless this bot's order carries a recorded attestation of the
    same kind -- and, for an order taken up by its Alpaca order id, the same
    id -- and its EXIT is no longer ``unknown``: an attestation whose
    settlement did not complete is not a receipt, and the order is settled
    again as presented.
    """
    row = repo.last_order_transition(order_ref=order_ref, transition_kind=ORDER_OUTCOME_ATTESTED)
    if row is None or row["strategy_instance_id"] != strategy_instance_id:
        return None
    facts = OrderOutcomeAttestedFacts.from_facts_json(row["facts_json"])
    if facts.attestation != attestation.kind or facts.broker_order_id != attestation.broker_order_id:
        return None
    effect = repo.effect_operation(row["effect_operation_id"])
    if effect is None or effect.state == "unknown":
        return None
    return UnknownOrderSettlementReceipt(
        sequence=row["sequence"],
        recorded_at_ms=row["recorded_at_ms"],
        order_ref=order_ref,
        attestation=attestation.kind,
    )


__all__ = [
    "ORDER_OUTCOME_ATTESTED",
    "AttestationFindings",
    "OrderOutcomeAttestation",
    "UnknownOrderSettlementReceipt",
    "UnknownOrderSettlementRefused",
    "check_unknown_exit_order",
    "recorded_settlement",
    "settle_unknown_exit_order",
]
