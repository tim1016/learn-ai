"""Where an account's money is: the one projection behind every money bar.

Formula (exact Decimal; every input is a fact the canonical budget projection
already read under one custody fence -- no FIFO, fee rule or balance here):
    C       observed cash
    U_buy   recorded buys (with their reported fees) that C does not show yet
    U_sell  recorded sale proceeds (net of their reported fees) C does not show yet
    cash  = C - U_buy + U_sell        -- a traded share is never also counted
                                         as cash, or as missing cash
    total = cash + sum(position cost) -- running bots, stopped bots, outside
    running bot = position cost + pending entries + max(free, 0)
    stopped bot = position cost + pending entries  (its positive free was released)
    outside     = position cost held by manual (operator) and external subjects
    charges     = fees owed that C does not show yet (``AccountBudget.fee_claims``)
    S           = ``AccountBudget.available`` + U_sell -- the unclaimed money
    settling    = min(U_sell, max(S, 0)) -- the unclaimed part still on its way
                  into cash: the canonical budget's own conservative holdback
                  (``available`` never counts proceeds C does not show)
    free        = max(``AccountBudget.available``, 0) -- Deploy's unreserved cash
    shortfall   = max(-S, 0) -- what the claims exceed the account by
    sum(segments) == total + shortfall exactly, because the projection's order
    claims are exactly (pending entries + U_buy); the identity is checked when
    the bar is drawn, never assumed.

A settlement window therefore never reads as a shortfall: until an exit's
proceeds reach cash, the budget holds them back from free to deploy and the
bar shows them settling; only claims beyond the settled cash are a shortfall.

Display (``money_bar``): each part rounds on its own -- half-even, except free
money, which floors exactly as the Deploy preview's unreserved cash does, and
the shortfall, which ceils as every requirement does -- a segment is the sum
of its part cents and ``total_cents`` the segment cents less the shortfall, so
the drawn dollars always add up. Widths are basis points by the canonical
largest-remainder rule (``money.apportion_units``).

Reference: https://github.com/tim1016/learn-ai/issues/2560 ("One account-money
  read (D12)"); money semantics are PRD #2540's, unchanged.
Canonical implementation: this file composes ``budgets.AccountBudget``;
  ``budgets.py`` stays the money formula.
Validated against: tests/broker/alpaca/clerk/test_account_money.py (worked
  examples: exact Decimal identity, cents and basis-point conservation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from app.broker.alpaca.clerk.budgets import AccountBudget, DeploymentBudget
from app.broker.alpaca.clerk.money import (
    ZERO,
    apportion_units,
    cents_required,
    cents_spendable,
    display_cents,
    money_context,
)

FULL_BAR_BPS = 10_000

SegmentKind = Literal["bot", "stopped", "outside", "charges", "settling", "new", "free"]


class MoneyConservationError(AssertionError):
    """The projected parts do not add up to the account: a bug, never a display."""


@dataclass(frozen=True)
class Holding:
    """A custody subject with no running budget that still has money in play.

    A bot subject (``strategy_instance_id`` set) is a stopped or pre-budget
    deployment; a manual (operator) or external subject is money held outside
    any bot.
    """

    subject_id: str
    strategy_instance_id: str | None
    position_cost: Decimal
    pending_orders: Decimal


@dataclass(frozen=True)
class StoppedHolding:
    strategy_instance_id: str
    position_cost: Decimal
    still_claimed: Decimal
    released: Decimal


@dataclass(frozen=True)
class AccountMoney:
    """Exact money map over one ``AccountBudget``.

    Charges are ``budget.fee_claims`` and free money is ``budget.available``,
    read from the budget itself so the bar and Deploy can never disagree.
    ``palette`` is each bot's stable colour slot: its place in the account's
    registration order. ``unvalued`` names holdings that have no place on the
    bar (a short position); they are named beside it, never silently dropped.
    """

    budget: AccountBudget
    cash: Decimal
    unseen_sales: Decimal
    running: tuple[DeploymentBudget, ...]
    stopped: tuple[StoppedHolding, ...]
    outside: Decimal
    total: Decimal
    palette: Mapping[str, int]
    unvalued: tuple[str, ...] = ()

    @property
    def holds_positions(self) -> bool:
        """Whether any share is held anywhere on the account, valued or not."""
        return bool(self.unvalued) or self.outside > ZERO or any(
            item.position_cost > ZERO for item in (*self.running, *self.stopped)
        )

    @property
    def unclaimed(self) -> Decimal:
        """``S``: cash, once every recorded sale has reached it, less every claim."""
        with money_context():
            return self.budget.available + self.unseen_sales


def account_money(
    budget: AccountBudget, *, unseen_fills: Decimal, unseen_sales: Decimal, holdings: Sequence[Holding],
    registration_order: Sequence[str], unvalued: Sequence[str] = (),
) -> AccountMoney:
    """Partition one budget projection into disjoint places for its money."""
    with money_context():
        stopped = [
            StoppedHolding(item.strategy_instance_id, item.position_cost, item.pending_orders, max(ZERO, item.free))
            for item in budget.deployments if not item.active
        ] + [
            StoppedHolding(item.strategy_instance_id, item.position_cost, item.pending_orders, ZERO)
            for item in holdings if item.strategy_instance_id is not None
        ]
        cash = budget.cash - unseen_fills + unseen_sales
        held = sum((item.position_cost for item in budget.deployments), ZERO) + sum((item.position_cost for item in holdings), ZERO)
        return AccountMoney(
            budget=budget,
            cash=cash,
            unseen_sales=unseen_sales,
            running=tuple(sorted((item for item in budget.deployments if item.active), key=lambda item: item.strategy_instance_id)),
            # A stopped bot that is flat with nothing still claimed is finished.
            stopped=tuple(sorted(
                (item for item in stopped if item.position_cost + item.still_claimed > ZERO),
                key=lambda item: item.strategy_instance_id,
            )),
            outside=sum((item.position_cost for item in holdings if item.strategy_instance_id is None), ZERO),
            total=cash + held,
            palette={sid: index for index, sid in enumerate(registration_order)},
            unvalued=tuple(unvalued),
        )


def _check_conservation(money: AccountMoney) -> None:
    """The exact identity every drawn bar rests on; a failure is a projection bug."""
    with money_context():
        budget = money.budget
        parts = (
            sum((item.position_cost + item.pending_orders + item.cash_claim for item in money.running), ZERO)
            + sum((item.position_cost + item.still_claimed for item in money.stopped), ZERO)
            + money.outside + budget.fee_claims + money.unclaimed
        )
        if parts != money.total:
            raise MoneyConservationError(
                f"The account's money parts sum to {parts}, not its total {money.total}."
            )


@dataclass(frozen=True)
class BarParts:
    """A running bot's slice, shaded: shares at cost, entry orders, free budget.

    Widths are of the slice itself; a slice with nothing in it draws no part
    (every width 0).
    """

    in_shares_cents: int
    in_shares_bps: int
    pending_cents: int
    pending_bps: int
    free_cents: int
    free_bps: int


@dataclass(frozen=True)
class BarSegment:
    kind: SegmentKind
    strategy_instance_id: str | None
    cents: int
    bps: int
    parts: BarParts | None = None
    shortfall_cents: int | None = None
    released_cents: int | None = None
    still_claimed_cents: int | None = None
    palette_index: int | None = None


@dataclass(frozen=True)
class MoneyBar:
    segments: tuple[BarSegment, ...]
    total_cents: int
    cash_cents: int
    shortfall_cents: int

    def cents_of(self, kind: SegmentKind) -> int:
        return sum(segment.cents for segment in self.segments if segment.kind == kind)

    def count_of(self, kind: SegmentKind) -> int:
        return sum(1 for segment in self.segments if segment.kind == kind)


def share_bps(cents: Mapping[str, int], *, empty: str | None) -> dict[str, int]:
    """Widths in basis points that sum to exactly 10000, or to 0 when nothing is drawn.

    Largest remainder over the cents (ties by key, the fee rule). A positive
    amount never draws as nothing: each one Hamilton rounds to zero takes one
    basis point from the widest part (ties by key). With nothing to draw at
    all, ``empty`` takes the whole width, or -- when ``empty`` is ``None`` --
    every width is 0, so an empty slice draws no part at all.
    """
    if any(value < 0 for value in cents.values()):
        raise ValueError("a money bar part cannot be negative")
    if not any(cents.values()):
        return {key: FULL_BAR_BPS if key == empty else 0 for key in cents}
    bps = apportion_units(FULL_BAR_BPS, {key: Decimal(value) for key, value in cents.items()})
    for key in sorted(key for key, value in cents.items() if value and not bps[key]):
        widest = min(bps, key=lambda other: (-bps[other], other))
        bps[widest] -= 1
        bps[key] = 1
    return bps


def bot_parts(item: DeploymentBudget) -> BarParts:
    """One deployment's slice, shaded; its widths are of the slice itself.

    Free budget floors exactly as the deployment's own spendable figure does;
    a stopped deployment's free was released, and an overrun has none, so
    neither draws a free part.
    """
    in_shares = display_cents(item.position_cost)
    pending = display_cents(item.pending_orders)
    free = cents_spendable(item.cash_claim)
    widths = share_bps({"in_shares": in_shares, "pending": pending, "free": free}, empty=None)
    return BarParts(in_shares, widths["in_shares"], pending, widths["pending"], free, widths["free"])


def shortfall_cents(item: DeploymentBudget) -> int:
    """What a deployment spent beyond its balance, shown, never a negative slice."""
    return cents_required(max(ZERO, -item.free))


def _bot_segment(item: DeploymentBudget, palette: Mapping[str, int]) -> BarSegment:
    parts = bot_parts(item)
    return BarSegment(
        kind="bot", strategy_instance_id=item.strategy_instance_id,
        cents=parts.in_shares_cents + parts.pending_cents + parts.free_cents, bps=0,
        parts=parts, shortfall_cents=shortfall_cents(item), palette_index=_palette_index(palette, item.strategy_instance_id),
    )


def _stopped_segment(item: StoppedHolding, palette: Mapping[str, int]) -> BarSegment:
    in_shares = display_cents(item.position_cost)
    still_claimed = display_cents(item.still_claimed)
    return BarSegment(
        kind="stopped", strategy_instance_id=item.strategy_instance_id, cents=in_shares + still_claimed, bps=0,
        released_cents=cents_spendable(item.released), still_claimed_cents=still_claimed,
        palette_index=_palette_index(palette, item.strategy_instance_id),
    )


def _palette_index(palette: Mapping[str, int], strategy_instance_id: str) -> int:
    index = palette.get(strategy_instance_id)
    if index is None:
        raise MoneyConservationError(f"Bot {strategy_instance_id!r} holds money but is not registered on this account.")
    return index


def money_bar(money: AccountMoney, *, new_cents: int | None = None) -> MoneyBar:
    """Draw ``money`` in cents and basis points, optionally carving a NEW slice.

    ``new_cents`` is a proposed commitment the caller already admitted: it is
    carved out of free to deploy, so every other slice and the total stay
    exactly as they are.
    """
    _check_conservation(money)
    budget = money.budget
    with money_context():
        unclaimed = money.unclaimed
        settling = min(money.unseen_sales, max(unclaimed, ZERO))
    # The Deploy preview's own unreserved figure, never a second rounding.
    free = budget.unreserved_cents
    if new_cents is not None and not 0 < new_cents <= free:
        raise ValueError("a proposed budget must be positive and within free to deploy")
    segments = [_bot_segment(item, money.palette) for item in money.running]
    segments += [_stopped_segment(item, money.palette) for item in money.stopped]
    if money.outside > ZERO:
        segments.append(BarSegment(kind="outside", strategy_instance_id=None, cents=display_cents(money.outside), bps=0))
    if budget.fee_claims > ZERO:
        segments.append(BarSegment(kind="charges", strategy_instance_id=None, cents=display_cents(budget.fee_claims), bps=0))
    if settling > ZERO:
        segments.append(BarSegment(kind="settling", strategy_instance_id=None, cents=display_cents(settling), bps=0))
    if new_cents is not None:
        segments.append(BarSegment(kind="new", strategy_instance_id=None, cents=new_cents, bps=0))
        free -= new_cents
    segments.append(BarSegment(kind="free", strategy_instance_id=None, cents=free, bps=0))
    # Stable identity breaks width ties: a bot's name, or the kind itself.
    keys = [segment.kind if segment.strategy_instance_id is None else f"{segment.kind}:{segment.strategy_instance_id}" for segment in segments]
    widths = share_bps(dict(zip(keys, (segment.cents for segment in segments), strict=True)), empty="free")
    drawn = tuple(replace(segment, bps=widths[key]) for key, segment in zip(keys, segments, strict=True))
    shortfall = cents_required(max(ZERO, -unclaimed))
    return MoneyBar(
        segments=drawn, total_cents=sum(segment.cents for segment in drawn) - shortfall,
        cash_cents=display_cents(money.cash), shortfall_cents=shortfall,
    )
