"""Whether one bot can fund one entry, which limit binds, and what it falls short by (#3097).

One answer for the Clerk's admission and for the bot page, so a screen can
never promise an entry the Clerk refuses. Every figure is whole cents. The
need rounds up and each limit rounds down, the boundary the Clerk already
acts at (``money.cents_required`` and ``money.cents_spendable``).

Formula: admits = need <= free budget and need <= account headroom;
  free budget      = committed + realized - fees - held positions at cost
                     - option collateral - working entry orders;
  account headroom = the account's unreserved cash + this bot's own claim
                     (``budgets.account_headroom``);
  binding          = the known limit of the three with the least room, ties
                     in the order bot budget, account headroom, broker
                     buying power;
  shortfall        = max(0, need - binding);  fits = max(0, binding) // need.
  Broker buying power never changes ``admits``; it can bind.
  A figure is stale when its reading is older than ``OBSERVATION_MAX_AGE_MS``;
  one that cannot be worked out is unknown, never zero and never a pass.
  A refusal that a known limit already makes does not wait on an unknown one.
Reference: PRD #3095 part B and issue #3097; ADR 0076 decision 28 for the one
  cash rule (#3096).
Canonical implementation: this file. The headroom is ``budgets.account_headroom``
  and is not derived a second time here.
Validated against: tests/broker/alpaca/clerk/test_funding.py (every figure
  worked by hand).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS, reading_is_fresh
from app.broker.alpaca.clerk.money import cents_required, cents_spendable, display_cents, money_context

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.budgets import AccountBudget, DeploymentBudget

FigureState = Literal["known", "stale", "unknown"]
LimitName = Literal["bot_budget", "account_headroom", "broker_buying_power"]

_BOT_BUDGET: LimitName = "bot_budget"
_ACCOUNT_HEADROOM: LimitName = "account_headroom"
_BROKER_BUYING_POWER: LimitName = "broker_buying_power"

_FIGURE_STATES: tuple[FigureState, ...] = ("known", "stale", "unknown")

_BOOKS = "Clerk books"
_BOOKS_AND_READING = "Clerk books and the account reading"
_ROUNDING = "Rounding to whole cents"

_STOPPED = "This deployment is stopped. Review a fresh Deploy before creating new exposure."
_NO_BUDGET = "This deployment has no budget on this account's books. Review a fresh Deploy before creating new exposure."
_BOOKS_UNREADABLE = "This bot's budget cannot be read right now, so what it can fund is unknown."
_NOTHING_FREE = "Nothing of its budget is free to trade, and an entry always costs something."
_NOTHING_FUNDABLE = "The account can fund nothing for this bot after protecting other positions, and an entry always costs something."
_PASS_NOTE = "Passing our own limits does not guarantee that Alpaca accepts the order."

_ACTION_BUDGET = "Stop the bot and deploy it again with a larger budget, or reduce its size."
_ACTION_HEADROOM = "Free another bot's budget, or add cash to the account."


def _require_cents(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} is a whole number of cents; got {value!r}")


@dataclass(frozen=True, slots=True)
class Term:
    """One labelled amount a figure is made of, in signed whole cents."""

    label: str
    amount_cents: int

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("a term carries its label")
        _require_cents(self.amount_cents, "a term")


@dataclass(frozen=True, slots=True)
class Figure:
    """One number the owner compares against another, with where it came from and how old it is.

    ``known`` and ``stale`` carry an amount whose ``terms`` add up to it
    exactly; ``unknown`` carries neither, and ``source`` says where it would
    come from. A stale amount is the last one worked out, never a current one.
    """

    amount_cents: int | None
    state: FigureState
    source: str
    as_of_ms: int | None = None
    terms: tuple[Term, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in _FIGURE_STATES:
            raise ValueError(f"a figure is known, stale or unknown; got {self.state!r}")
        if not self.source:
            raise ValueError("a figure says where it came from")
        if self.state == "unknown":
            if self.amount_cents is not None or self.terms:
                raise ValueError("an unknown figure states no amount and no terms: unknown is never zero")
            return
        _require_cents(self.amount_cents, "a figure's amount")
        if sum(term.amount_cents for term in self.terms) != self.amount_cents:
            raise ValueError(f"a figure's terms add up to its amount; {self.terms!r} do not make {self.amount_cents}")

    @classmethod
    def unknown(cls, source: str) -> Figure:
        return cls(amount_cents=None, state="unknown", source=source)

    @property
    def is_known(self) -> bool:
        return self.state == "known"


@dataclass(frozen=True, slots=True)
class EntryNeed:
    """What one entry commits, in whole cents, and the parts that make it."""

    commitment_cents: int
    terms: tuple[Term, ...]

    def __post_init__(self) -> None:
        _require_cents(self.commitment_cents, "an entry commitment")
        if self.commitment_cents < 0:
            raise ValueError(f"an entry commitment is zero or more; got {self.commitment_cents}")
        if sum(term.amount_cents for term in self.terms) != self.commitment_cents:
            raise ValueError(f"an entry's parts add up to its commitment; {self.terms!r} do not make {self.commitment_cents}")

    @classmethod
    def for_shares(cls, *, requirement: Decimal, fee_provision_cents: int) -> EntryNeed:
        """A position in shares: its exact notional plus fee provision (``budgets.entry_requirement``), rounded up to the cent once."""
        commitment_cents = cents_required(requirement)
        return cls(
            commitment_cents=commitment_cents,
            terms=(
                Term("Shares at the ask", commitment_cents - fee_provision_cents),
                Term("Fee provision", fee_provision_cents),
            ),
        )

    @classmethod
    def for_options(cls, *, commitment_cents: int, fee_provision_cents: int) -> EntryNeed:
        """An entry in option contracts as admission is handed it: the requirement and the signed premium together, and the fee provision."""
        return cls(
            commitment_cents=commitment_cents,
            terms=(
                Term("Broker requirement and net premium", commitment_cents - fee_provision_cents),
                Term("Fee provision", fee_provision_cents),
            ),
        )


@dataclass(frozen=True, slots=True)
class FundingAnswer:
    """Whether the bot can fund the entry, by which limit, and the words for it.

    ``admits`` is the Clerk's own rule and is ``None`` when there is no need
    or a limit it depends on is not known. ``binding`` is the known limit with
    the least room. ``shortfall_cents`` and ``fits`` are ``None`` when they
    cannot be said. ``action`` is given only where it is known to work.
    """

    free_budget: Figure
    account_headroom: Figure
    broker_buying_power: Figure | None
    need: EntryNeed | None
    admits: bool | None
    binding: LimitName | None
    shortfall_cents: int | None
    fits: int | None
    sentence: str
    action: str | None


def funding_answer(
    account: AccountBudget | None,
    *,
    strategy_instance_id: str,
    need: EntryNeed | None,
    broker_buying_power: Figure | None,
    observed_at_ms: int,
    now_ms: int,
) -> FundingAnswer:
    """Whether ``strategy_instance_id`` can fund ``need`` out of ``account``, as of ``now_ms``.

    ``account`` is ``None`` when the budget could not be worked out: every
    figure is then unknown. ``observed_at_ms`` is when the account reading
    behind ``account`` was taken and ``now_ms`` when this is asked; the
    reading is stale past ``OBSERVATION_MAX_AGE_MS``. ``need`` is ``None``
    for an entry not priced yet, which still shows the limits.
    ``broker_buying_power`` is the caller's figure, or ``None`` where the bot
    has none (a Dry Run, a Live Shadow); it never changes ``admits``.
    """
    with money_context():
        own = None if account is None else next(
            (item for item in account.deployments if item.strategy_instance_id == strategy_instance_id), None,
        )
        free = _free_budget(own, now_ms=now_ms)
        headroom = _headroom(account, own, observed_at_ms=observed_at_ms, now_ms=now_ms)
        broker = _aged(broker_buying_power, now_ms=now_ms)
        if own is not None and not own.active:
            return FundingAnswer(
                free, headroom, broker, need, admits=False, binding=None, shortfall_cents=None, fits=None,
                sentence=_STOPPED, action=None,
            )
        room = _Room(_known_cents(free), _known_cents(headroom), _known_cents(broker))
        admits = _admits(room, need)
        binding = room.binding()
        shortfall, fits = _shortfall_and_fits(need, admits, binding)
        name = None if binding is None else binding[0]
        return FundingAnswer(
            free, headroom, broker, need, admits=admits, binding=name, shortfall_cents=shortfall, fits=fits,
            sentence=_sentence(
                account_read=account is not None, own_found=own is not None, room=room,
                headroom_is_stale=headroom.state == "stale", need=need, admits=admits, binding=name,
                shortfall_cents=shortfall,
            ),
            action=_action(admits, name),
        )


def _free_budget(own: DeploymentBudget | None, *, now_ms: int) -> Figure:
    if own is None:
        return Figure.unknown(f"{_BOOKS}, which hold no budget for this bot")
    if not own.active:
        return Figure(0, "known", _BOOKS, now_ms, (Term("Released when the bot stopped", 0),))
    amount = cents_spendable(own.free)
    collateral = own.option_collateral
    exact = [
        ("Budget set aside at deploy", Decimal(own.committed_cents) / 100),
        ("Realized gains and losses", own.realized_gross),
        ("Fees", -own.fees),
        ("Held positions at cost", -(own.position_cost - collateral)),
    ]
    if collateral:
        exact.append(("Collateral held against sold options", -collateral))
    exact.append(("Waiting in entry orders", -own.pending_orders))
    return Figure(amount, "known", _BOOKS, now_ms, _terms_adding_to(amount, exact))


def _headroom(
    account: AccountBudget | None, own: DeploymentBudget | None, *, observed_at_ms: int, now_ms: int,
) -> Figure:
    if account is None or own is None:
        return Figure.unknown(f"{_BOOKS_AND_READING}, which could not be read for this bot")
    # budgets.py's entry decisions call this module, so its headroom is imported at the call.
    from app.broker.alpaca.clerk.budgets import account_headroom

    amount = cents_spendable(account_headroom(account, own))
    exact = [("Unreserved account cash", account.available), ("This bot's own claim", own.cash_claim)]
    fresh = reading_is_fresh(observed_at_ms, now_ms=now_ms, max_age_ms=OBSERVATION_MAX_AGE_MS)
    return Figure(amount, "known" if fresh else "stale", _BOOKS_AND_READING, observed_at_ms, _terms_adding_to(amount, exact))


def _aged(figure: Figure | None, *, now_ms: int) -> Figure | None:
    """A caller's figure under the same freshness limit as the account reading."""
    if figure is None or not figure.is_known or figure.as_of_ms is None:
        return figure
    if reading_is_fresh(figure.as_of_ms, now_ms=now_ms, max_age_ms=OBSERVATION_MAX_AGE_MS):
        return figure
    return replace(figure, state="stale")


def _terms_adding_to(amount_cents: int, exact: Sequence[tuple[str, Decimal]]) -> tuple[Term, ...]:
    """Each exact term rounded to the cent, and one visible rounding term when they would not add up to ``amount_cents``."""
    terms = [Term(label, display_cents(value)) for label, value in exact]
    drift = amount_cents - sum(term.amount_cents for term in terms)
    if drift:
        terms.append(Term(_ROUNDING, drift))
    return tuple(terms)


def _known_cents(figure: Figure | None) -> int | None:
    """The amount of a known figure; a stale or unknown one is not a room to judge by."""
    return figure.amount_cents if figure is not None and figure.is_known else None


@dataclass(frozen=True, slots=True)
class _Room:
    """What each limit leaves, in cents, where it is known."""

    free: int | None
    headroom: int | None
    broker: int | None

    def binding(self) -> tuple[LimitName, int] | None:
        """The known limit with the least room; ``min`` keeps the first of equals, which is the tie order."""
        known = [
            (name, cents)
            for name, cents in (
                (_BOT_BUDGET, self.free), (_ACCOUNT_HEADROOM, self.headroom), (_BROKER_BUYING_POWER, self.broker),
            )
            if cents is not None
        ]
        return min(known, key=lambda item: item[1]) if known else None


def _admits(room: _Room, need: EntryNeed | None) -> bool | None:
    """The Clerk's rule on the bot's own two limits.

    With no need an entry still costs at least a cent (an entry commitment is
    above zero), so a known limit with no room admits nothing, and anything
    else says nothing. A refusal a known limit makes stands whatever the
    other is; a pass needs both known.
    """
    required = 1 if need is None else need.commitment_cents
    if any(cents is not None and cents < required for cents in (room.free, room.headroom)):
        return False
    if need is None or room.free is None or room.headroom is None:
        return None
    return True


def _shortfall_and_fits(
    need: EntryNeed | None, admits: bool | None, binding: tuple[LimitName, int] | None,
) -> tuple[int | None, int | None]:
    """The need less the binding limit and the whole entries it covers; ``None`` where that cannot be said."""
    if need is None or admits is None or binding is None:
        return None, None
    room = binding[1]
    fits = max(0, room) // need.commitment_cents if need.commitment_cents > 0 else None
    return max(0, need.commitment_cents - room), fits


def _action(admits: bool | None, binding: LimitName | None) -> str | None:
    """What the owner can do, for the two limits that are theirs to move."""
    if admits is not False:
        return None
    if binding == _BOT_BUDGET:
        return _ACTION_BUDGET
    if binding == _ACCOUNT_HEADROOM:
        return _ACTION_HEADROOM
    return None


def _usd(cents: int) -> str:
    dollars, remainder = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}${dollars:,}.{remainder:02d}"


def _has_free(cents: int, *, only: bool = False) -> str:
    if cents < 0:
        return f"is {_usd(-cents)} over its budget"
    return f"has {'only ' if only else ''}{_usd(cents)} free"


def _account_can_fund(cents: int, *, only: bool = False) -> str:
    if cents < 0:
        return f"the account can fund nothing for it after protecting other positions, because it is {_usd(-cents)} over-committed"
    return f"the account can fund {'only ' if only else ''}{_usd(cents)} after protecting other positions"


def _sentence(
    *, account_read: bool, own_found: bool, room: _Room, headroom_is_stale: bool, need: EntryNeed | None,
    admits: bool | None, binding: LimitName | None, shortfall_cents: int | None,
) -> str:
    """The one sentence the owner reads. The caller has settled that a deployment found is running."""
    if not account_read:
        return _BOOKS_UNREADABLE
    if not own_found:
        return _NO_BUDGET
    assert room.free is not None, "a running deployment's own books always give its free budget"
    stale = (
        f" The account's headroom for it was last read more than {OBSERVATION_MAX_AGE_MS // 1000} seconds ago, "
        "so it is not counted here."
        if headroom_is_stale else ""
    )
    free, headroom = room.free, room.headroom
    if need is None:
        if admits is False:
            return (_NOTHING_FREE if free < 1 else _NOTHING_FUNDABLE) + stale
        limits = f"This bot {_has_free(free)}"
        if headroom is not None:
            limits += f" and {_account_can_fund(headroom)}"
        return f"{limits}.{stale}"
    needs = f"The entry needs {_usd(need.commitment_cents)} including its fee provision."
    if admits is None:
        return f"This bot {_has_free(free)}.{stale} Whether the entry fits is not said. {needs}"
    shortfall = f" Shortfall: {_usd(shortfall_cents)}." if shortfall_cents else ""
    if headroom is None:
        # A refusal by the budget alone: the account's figure is not known, and the stale note says so.
        return f"This bot {_has_free(free, only=True)}. {needs}{shortfall}{stale}"
    if admits:
        sentence = (
            f"This bot {_has_free(free)} and {_account_can_fund(headroom)}, "
            f"enough for the entry's {_usd(need.commitment_cents)} including its fee provision."
        )
        if binding == _BROKER_BUYING_POWER and room.broker is not None and shortfall_cents:
            sentence += f" Alpaca's option buying power is only {_usd(room.broker)}, so Alpaca would likely refuse the order.{shortfall}"
        return f"{sentence} {_PASS_NOTE}"
    if binding == _ACCOUNT_HEADROOM:
        limits = f"This bot {_has_free(free)}, but {_account_can_fund(headroom, only=True)}."
    elif binding == _BROKER_BUYING_POWER and room.broker is not None:
        limits = (
            f"This bot {_has_free(free)} and {_account_can_fund(headroom)}, "
            f"but Alpaca's option buying power is only {_usd(room.broker)}."
        )
    else:
        limits = f"This bot {_has_free(free, only=True)}."
        if headroom < need.commitment_cents:
            can_fund = "nothing" if headroom < 0 else f"only {_usd(headroom)}"
            limits += f" The account can fund {can_fund} for it after protecting other positions."
    return f"{limits} {needs}{shortfall}"
