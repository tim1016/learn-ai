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
Alpaca does not hold. The EXIT fails releasably, under an ``EXIT_NOT_FLAT``
notice, and the attributed quantity is never changed.

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

One ``ORDER_OUTCOME_ATTESTED`` transition records who attested what and
what the Clerk read, and its own fold settles the order: an ``absent`` one
ends the EXIT and opens the notice it ends under, an ``exists`` one records
the order's Alpaca order id on the order. So the ledger never holds an
attestation that did not take effect, or an effect with no attestation,
wherever the process stops. What an order taken up by its id then did -- a
fill, its ending -- is folded in writes of their own, as any answer about
the Clerk's own order is; a process that stops between those leaves an order
the Clerk's next pass reads by that id and finishes.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from app.broker.alpaca.clerk.sqlite.account_open_work import (
    MAX_OPEN_ORDER_SNAPSHOT,
    broker_order_in_flight,
    open_order_snapshot_is_full,
    read_open_orders_then_positions,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    ORDER_ATTESTED_ABSENT,
    attested_absent_exit_notice,
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
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    resolve_exit_not_flat_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    POSITION_DRIFT_REASON_CODE,
    PositionDriftCause,
)
from app.broker.contract.errors import BrokerError, BrokerRequestInvalid
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
        if self.kind == "exists" and not _is_alpaca_order_id(self.broker_order_id):
            raise ValueError(
                "An attestation that Alpaca shows the order names its Alpaca order id: a UUID, "
                "lower case with hyphens, as Alpaca answers with it"
            )
        if self.kind == "absent" and self.broker_order_id is not None:
            raise ValueError("An attestation that Alpaca has no such order names no Alpaca order id")


def _is_alpaca_order_id(broker_order_id: str | None) -> bool:
    """Whether ``broker_order_id`` is a UUID as Alpaca spells an order id.

    The spelling matters: the Clerk checks that the order Alpaca returns
    carries the id it asked for, and records that id on the order.
    """
    if broker_order_id is None:
        return False
    try:
        return str(UUID(broker_order_id)) == broker_order_id
    except ValueError:
        return False


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
class _AbsentReads:
    """What the Clerk read at Alpaca for "no such order": two rounds, the second after the first returned."""

    rounds: tuple[_AccountRead, _AccountRead]
    since_ms: int
    observed_at_ms: int


@dataclass(frozen=True)
class _OrderRead:
    """What the Clerk read at Alpaca for "it is the order under this id": that order."""

    observed: BrokerOrder
    observed_at_ms: int


type _AlpacaRead = _AbsentReads | _OrderRead


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
class _Operator:
    """Who settles the order, and why."""

    operator: str
    operator_reason: str | None


def _no_claim_to_renew() -> None:
    """A check holds no claim: it settles nothing, so it has nothing to keep."""


@dataclass(frozen=True)
class _AlpacaReads:
    """Alpaca as an attestation reads it: reads only, and each one refuses when it fails.

    ``around_each_read`` runs before and after every read. A settlement
    renews the EXIT's claim there, as every broker contact of a resolution
    does, so one that lost its claim reads no further and commits nothing. A
    check holds no claim. Nothing here can send or cancel an order.
    """

    read: BrokerReadPort
    trade: BrokerTradePort
    around_each_read: Callable[[], None]

    async def _answer[T](self, ask: Callable[[], Awaitable[T]]) -> T:
        self.around_each_read()
        try:
            return await ask()
        except BrokerError as error:
            raise _broker_unreadable(error) from error
        finally:
            self.around_each_read()

    async def order_sent_as(self, client_order_id: str) -> BrokerOrder | None:
        """The order's exact lookup by the client order id it was sent under."""
        observed = await self._answer(lambda: self.trade.get_order_by_client_order_id(client_order_id))
        if observed is not None and observed.client_order_id != client_order_id:
            raise _broker_unreadable(
                BrokerError(
                    f"broker returned client_order_id={observed.client_order_id!r}, "
                    f"expected {client_order_id!r}"
                )
            )
        return observed

    async def order_with_id(self, broker_order_id: str) -> BrokerOrder | None:
        """The order Alpaca has under ``broker_order_id``; ``None`` for an id it has no order under."""
        self.around_each_read()
        try:
            observed = await self.trade.get_order_by_broker_order_id(broker_order_id)
        except BrokerRequestInvalid:
            # An id Alpaca could not take as an order id names no order it has.
            return None
        except BrokerError as error:
            raise _broker_unreadable(error) from error
        finally:
            self.around_each_read()
        if observed is not None and observed.order_id != broker_order_id:
            raise _broker_unreadable(
                BrokerError(f"broker returned order_id={observed.order_id!r}, expected {broker_order_id!r}")
            )
        return observed

    async def open_orders_then_positions(self) -> tuple[list[BrokerOrder], list[BrokerPosition]]:
        open_orders, positions = await self._answer(lambda: read_open_orders_then_positions(self.read))
        if isinstance(positions, BrokerError):
            raise _broker_unreadable(positions)
        return open_orders, positions

    async def orders_since(self, after_ms: int) -> list[BrokerOrder]:
        """Every order the account was sent after ``after_ms``, in any state.

        One page of the most a read returns, newest first; a full page may
        not be all of them, and the proof refuses on one.
        """
        return await self._answer(
            lambda: self.read.list_orders(status="all", limit=MAX_OPEN_ORDER_SNAPSHOT, after_ms=after_ms)
        )


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
    before the claim is taken and again as the settlement commits, and must
    still be that order. ``operator`` is who attests, recorded with their
    reason. Raises :class:`UnknownOrderSettlementRefused`, having written
    nothing, whenever the Clerk's reads do not bear the attestation out.

    Everything the durable records alone refuse is refused first, before the
    EXIT is claimed and before Alpaca is asked anything.
    """
    if not operator or len(operator) > 64:
        raise UnknownOrderSettlementRefused(
            "OPERATOR_UNKNOWN",
            "A settled order records who settled it, and no operator identity was given. "
            "Nothing was changed.",
        )
    by = _Operator(operator=operator, operator_reason=(operator_reason or "").strip() or None)
    _require_a_broker_account(trade)
    await to_thread(
        lambda: _require_settleable(
            repo, strategy_instance_id=strategy_instance_id, presented=presented, kind=attestation.kind
        )
    )
    try:
        claim_token = repo.claim_before_broker_contact(presented.effect_operation_id).token
    except OperationClaimError as busy:
        raise _exit_is_busy() from busy

    def renew_the_claim() -> None:
        if not repo.renew_operation_claim(
            effect_operation_id=presented.effect_operation_id, token=claim_token
        ):
            raise OperationClaimError(
                f"effect_operation {presented.effect_operation_id!r} lost its broker-contact claim"
            )

    try:
        alpaca_read = await _read_alpaca(
            _AlpacaReads(read=read, trade=trade, around_each_read=renew_the_claim),
            presented,
            attestation,
            clock=repo.clock,
        )
        return await intake.off_loop(
            _commit,
            repo,
            strategy_instance_id=strategy_instance_id,
            presented=presented,
            alpaca_read=alpaca_read,
            by=by,
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
    so what an operator is shown before they confirm is what a settlement
    made now would rest on; the settlement reads it all again. A refusal is
    an answer here, not an error.

    It is a read: it takes no claim on the EXIT and writes nothing, so a
    check in flight never makes a pass of the Clerk defer the EXIT, or a
    settlement find it busy.
    """
    try:
        _require_a_broker_account(trade)
        await to_thread(
            lambda: _require_settleable(
                repo, strategy_instance_id=strategy_instance_id, presented=presented, kind=attestation.kind
            )
        )
        alpaca_read = await _read_alpaca(
            _AlpacaReads(read=read, trade=trade, around_each_read=_no_claim_to_renew),
            presented,
            attestation,
            clock=repo.clock,
        )
        return await intake.off_loop(
            _findings,
            repo,
            strategy_instance_id=strategy_instance_id,
            presented=presented,
            alpaca_read=alpaca_read,
        )
    except UnknownOrderSettlementRefused as refused:
        return AttestationFindings(
            attestation=attestation.kind,
            would_settle=False,
            refusal_reason_code=refused.reason_code,
            lines=(str(refused),),
            observed_at_ms=repo.clock(),
        )


def _require_a_broker_account(trade: BrokerTradePort) -> None:
    if trade_port_folds_simulated_evidence(trade):
        raise UnknownOrderSettlementRefused(
            "NOT_A_BROKER_ACCOUNT",
            "This bot trades in a simulation, which has no account at Alpaca to look at. "
            "Nothing was changed.",
        )


async def _read_alpaca(
    alpaca: _AlpacaReads,
    presented: ProjectedUnknownExitOrder,
    attestation: OrderOutcomeAttestation,
    *,
    clock: Callable[[], int],
) -> _AlpacaRead:
    """Everything the Clerk reads at Alpaca for ``attestation``, dated when the last read returned."""
    if attestation.kind == "exists":
        assert attestation.broker_order_id is not None
        observed = await alpaca.order_with_id(attestation.broker_order_id)
        if observed is None:
            raise UnknownOrderSettlementRefused(
                "NO_SUCH_BROKER_ORDER",
                f"Alpaca has no order with the id {attestation.broker_order_id!r}. Check the id at "
                "Alpaca. Nothing was changed.",
            )
        return _OrderRead(observed=observed, observed_at_ms=clock())
    since_ms = presented.created_at_ms - ORDERS_SINCE_MARGIN_MS
    rounds = (
        await _read_account(alpaca, presented, since_ms=since_ms),
        await _read_account(alpaca, presented, since_ms=since_ms),
    )
    return _AbsentReads(rounds=rounds, since_ms=since_ms, observed_at_ms=clock())


def _exit_is_busy() -> UnknownOrderSettlementRefused:
    return UnknownOrderSettlementRefused(
        "EXIT_BUSY",
        "The Clerk is working on this exit right now. Try again in a few seconds. "
        "Nothing was changed.",
    )


def _require_settleable(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    kind: SettlementAttestation,
) -> None:
    """Refuse on what the Clerk's own records say, before Alpaca is asked anything.

    The order must still be the one the catalog presented, read through the
    catalog's own read (``unknown_exit_orders``), so what is checked here is
    what was offered: an order the Clerk sent again since compares unequal,
    its latest send being part of it. Its bot must still be stopped. And
    "no such order" waits out the submit-absence wait after the latest send:
    an order sent moments ago may be at Alpaca and not yet show in a read,
    which the Clerk's own re-send waits out too.
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
    if kind != "absent":
        return
    settles_at_ms = unknown_order_lookup_settles_at_ms(presented)
    now_ms = repo.clock()
    if now_ms < settles_at_ms:
        raise UnknownOrderSettlementRefused(
            "ORDER_LOOKUP_STILL_SETTLING",
            "The Clerk sent this order moments ago, and Alpaca may not show it yet. Settle again "
            f"after {et_when_words(settles_at_ms, now_ms=now_ms)}. Nothing was changed.",
        )


async def _read_account(
    alpaca: _AlpacaReads, presented: ProjectedUnknownExitOrder, *, since_ms: int
) -> _AccountRead:
    """One round: the exact lookup, the open orders, then the positions, then every order since ``since_ms``.

    In that sequence an order that arrives between two reads of a round is
    in a later one: among the open orders if it works, among the orders
    since if it has ended. Any read that fails refuses; so does the order's
    own exact lookup answering.
    """
    exact = await alpaca.order_sent_as(presented.order_ref)
    if exact is not None:
        raise _order_is_at_alpaca(exact)
    open_orders, positions = await alpaca.open_orders_then_positions()
    return _AccountRead(
        open_orders=open_orders, positions=positions, orders_since=await alpaca.orders_since(since_ms)
    )


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


def _prove(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    alpaca_read: _AlpacaRead,
) -> _AbsenceProof | None:
    """Refuse unless the Clerk's records and what it read at Alpaca bear the attestation out. Writes nothing.

    Run inside one fenced run by both callers, so every durable record is
    read as one state. The proof of an absent order is returned; an order
    Alpaca shows needs none beyond being this order.
    """
    if isinstance(alpaca_read, _AbsentReads):
        _require_settleable(
            repo, strategy_instance_id=strategy_instance_id, presented=presented, kind="absent"
        )
        return _prove_absent(repo, presented, alpaca_read.rounds)
    _require_settleable(
        repo, strategy_instance_id=strategy_instance_id, presented=presented, kind="exists"
    )
    _require_same_order(presented, alpaca_read.observed)
    return None


def _findings(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    alpaca_read: _AlpacaRead,
) -> AttestationFindings:
    """What a check answers when a settlement made now would go through: the Clerk's own sentences of what it read."""
    proof = _prove(
        repo, strategy_instance_id=strategy_instance_id, presented=presented, alpaca_read=alpaca_read
    )
    if isinstance(alpaca_read, _AbsentReads):
        assert proof is not None
        return AttestationFindings(
            attestation="absent",
            would_settle=True,
            refusal_reason_code=None,
            lines=_absence_lines(presented, proof),
            observed_at_ms=alpaca_read.observed_at_ms,
        )
    observed = alpaca_read.observed
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
        observed_at_ms=alpaca_read.observed_at_ms,
    )


def _commit(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    presented: ProjectedUnknownExitOrder,
    alpaca_read: _AlpacaRead,
    by: _Operator,
) -> UnknownOrderSettlementReceipt:
    """The fenced run of a settlement: every durable check again, then the settlement."""
    proof = _prove(
        repo, strategy_instance_id=strategy_instance_id, presented=presented, alpaca_read=alpaca_read
    )
    if isinstance(alpaca_read, _AbsentReads):
        assert proof is not None
        return _commit_absent(repo, presented=presented, reads=alpaca_read, proof=proof, by=by)
    return _commit_order(repo, presented=presented, order_read=alpaca_read, by=by)


def _commit_absent(
    repo: ClerkSqliteRepository,
    *,
    presented: ProjectedUnknownExitOrder,
    reads: _AbsentReads,
    proof: _AbsenceProof,
    by: _Operator,
) -> UnknownOrderSettlementReceipt:
    """End the EXIT of an order the Clerk's own reads bear out as absent: one write."""
    effect = repo.effect_operation(presented.effect_operation_id)
    assert effect is not None
    notice = attested_absent_exit_notice(
        repo,
        effect_operation_id=presented.effect_operation_id,
        order_ref=presented.order_ref,
        broker_qty=proof.broker_qty,
        account_attributed_qty=proof.account_attributed_qty,
    )
    open_notice = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=effect.strategy_instance_id,
    )
    # The one write of this settlement: its own fold ends the EXIT and opens
    # the notice it ends under, so no ledger holds a part of it.
    committed = _append_attestation(
        repo,
        presented=presented,
        facts=OrderOutcomeAttestedFacts(
            attestation="absent",
            operator=by.operator,
            operator_reason=by.operator_reason,
            client_order_id=presented.order_ref,
            symbol=presented.symbol,
            side=presented.side,
            quantity=presented.quantity,
            latest_send_sequence=presented.latest_send_sequence,
            uncertainty_id=presented.uncertainty_id,
            broker_observed_at_ms=reads.observed_at_ms,
            open_orders_read=proof.open_orders_read,
            orders_since_read=proof.orders_since_read,
            orders_read_since_ms=reads.since_ms,
            broker_qty=proof.broker_qty,
            account_attributed_qty=proof.account_attributed_qty,
            exit_not_flat=None if notice is None else json.loads(notice.to_facts_json()),
        ),
        observed_at_ms=reads.observed_at_ms,
        operation_state="succeeded" if notice is None else "failed",
        summary_code=ORDER_ATTESTED_ABSENT,
        # A notice already open is refreshed, and joined to its own timeline
        # by this reference, as every refresh is.
        proof_reference=(
            None if notice is None or open_notice is None else open_notice["uncertainty_id"]
        ),
    )
    if notice is None:
        # The bot holds nothing: the EXIT ended attributed-flat, and a notice
        # of an earlier exit that left the position open no longer stands. A
        # pass of the Clerk resolves it too, should this write be lost.
        resolve_exit_not_flat_uncertainty(
            repo,
            strategy_instance_id=effect.strategy_instance_id,
            evidence_refs=(presented.order_ref,),
        )
    return _settled(repo, committed, presented, by, attestation="absent")


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
    operation_state: str,
    summary_code: str,
    proof_reference: str | None = None,
) -> CommittedTransition:
    """Record who attested what, and what the Clerk read, on the EXIT's own timeline.

    The row's own fold settles the order (``folds._fold_order_outcome_attested``).
    ``operation_state`` is the state that fold leaves the EXIT in.
    """
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
            operation_state=operation_state,
            clerk_observed_at_ms=observed_at_ms,
            summary_code=summary_code,
            proof_reference=proof_reference,
            facts_json=facts.to_facts_json(),
        )
    )


def _settled(
    repo: ClerkSqliteRepository,
    committed: CommittedTransition,
    presented: ProjectedUnknownExitOrder,
    by: _Operator,
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
            "operator": by.operator,
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


def _commit_order(
    repo: ClerkSqliteRepository,
    *,
    presented: ProjectedUnknownExitOrder,
    order_read: _OrderRead,
    by: _Operator,
) -> UnknownOrderSettlementReceipt:
    """Take up the order Alpaca shows as this EXIT's own: one write, then the order's own evidence."""
    observed = order_read.observed
    committed = _append_attestation(
        repo,
        presented=presented,
        facts=OrderOutcomeAttestedFacts(
            attestation="exists",
            operator=by.operator,
            operator_reason=by.operator_reason,
            client_order_id=presented.order_ref,
            symbol=presented.symbol,
            side=presented.side,
            quantity=presented.quantity,
            latest_send_sequence=presented.latest_send_sequence,
            uncertainty_id=presented.uncertainty_id,
            broker_observed_at_ms=order_read.observed_at_ms,
            broker_order_id=observed.order_id,
            broker_state=observed.status,
            broker_filled_quantity=observed.filled_quantity,
        ),
        observed_at_ms=order_read.observed_at_ms,
        operation_state="unknown",
        summary_code=ORDER_OUTCOME_ATTESTED,
    )
    # The write above took the order up: it carries its broker order id now.
    # What that order did is folded as the Clerk folds any answer about its
    # own order, in writes that each commit alone. Should the process stop
    # between them, the Clerk's next pass reads the order by that id and
    # folds what is left (``exit_resolution._observe_by_broker_order_id``).
    fold_exit_attested_order(
        repo, effect_operation_id=presented.effect_operation_id, observed=observed
    )
    return _settled(repo, committed, presented, by, attestation="exists")


def recorded_settlement(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    order_ref: str,
    attestation: OrderOutcomeAttestation,
) -> UnknownOrderSettlementReceipt | None:
    """The receipt of this same settlement, when it was already made: a replay does nothing twice.

    ``None`` unless this bot's order carries a recorded attestation of the
    same kind and, for an order taken up by its Alpaca order id, the same id.
    That row is the settlement itself -- its own fold ended the EXIT, or
    recorded the order's broker order id -- so it is on the ledger only when
    this attestation took effect. An order that left ``unknown`` any other
    way carries no such row, and is never answered with a receipt.
    """
    row = repo.last_order_transition(order_ref=order_ref, transition_kind=ORDER_OUTCOME_ATTESTED)
    if row is None or row["strategy_instance_id"] != strategy_instance_id:
        return None
    facts = OrderOutcomeAttestedFacts.from_facts_json(row["facts_json"])
    if facts.attestation != attestation.kind or facts.broker_order_id != attestation.broker_order_id:
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
