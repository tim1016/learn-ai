"""Whether one bot can fund one entry, which limit binds, and what it falls short by (#3097).

One answer for the Clerk's admission and for the bot page, so the same entry
at the same moment is never promised by a screen and refused by the Clerk.
Every figure is whole cents. The need rounds up and each limit rounds down
(``money.cents_required`` and ``money.cents_spendable``), the cent boundary
Deploy acts at. The entry test compared exact decimals before #3097, so an
entry that fits a limit only within the same cent is now refused; none is
admitted that the exact comparison would have refused.

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
  A refusal is bound by a limit that makes it, short by what that limit must
  cover (the budget covers the entry; at a Deploy the account funds the
  entry and the whole budget set aside), the one short by more when two
  refuse; a figure that covers what it must is never named. Only an entry no
  limit refuses names the limit with the least room, which Alpaca's own
  figure can be.
  A figure is stale when its reading is older than ``OBSERVATION_MAX_AGE_MS``;
  one that cannot be worked out is unknown, never zero and never a pass.
  A refusal that a known limit already makes does not wait on an unknown one.
  ``deploy_funding_answer`` is the same answer for a bot that does not exist
  yet (#3099): the free budget is the amount typed and the headroom is the
  account's unreserved cash. The budget is set aside out of that cash, so the
  account must also fund all of it (admits needs need <= budget <= unreserved
  cash), and a refusal by that limit is short by max(need, budget) less the
  cash. How many fit is the least room over the need, a whole number, said
  with the caveat that every bot draws on the same headroom.
Reference: #3095 part B and issues #3097 and #3099; ADR 0076 decision 28
  for the one cash rule (#3096).
Canonical implementation: this file. The headroom is ``budgets.account_headroom``
  and is not derived a second time here.
Validated against: tests/broker/alpaca/clerk/test_funding.py and
  tests/broker/alpaca/clerk/test_deploy_funding.py (every figure worked by hand).
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

_ALPACA_ACCOUNT = "Alpaca's account answer"
_HOLDINGS = "The option contracts the books attribute to this account's bots, with the contracts the preview picked"
_TYPED_BUDGET = "The budget typed in Deploy"
_SIMULATED = "A Dry Run's private simulated cash"
_DEPLOY_UNREAD = "This account's money cannot be read right now, so what it can fund is unknown."
_ACTION_DEPLOY_ACCOUNT = "Choose a smaller budget, free another bot's budget, or add cash to the account."
_LIMIT_WORDS: dict[LimitName, str] = {
    "bot_budget": "the bot's budget",
    "account_headroom": "the account's headroom",
    "broker_buying_power": "Alpaca's option buying power",
}


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
    or a limit it depends on is not known. ``binding`` is the limit that
    refuses the entry when one does, and otherwise the known limit with the
    least room.
    ``shortfall_cents`` and ``fits`` are ``None`` when they cannot be said.
    ``action`` is given only where it is known to work.
    ``fits_sentence`` says ``fits`` with the caveat that every bot draws on
    the same account headroom. ``budget_left`` and ``headroom_left`` are what
    each limit leaves after one entry, given only by ``deploy_funding_answer``;
    a figure keeps its limit's state, so a stale limit leaves a stale one.
    ``simulated`` marks a Dry Run's, whose cash is its own: no account limit
    and no other bot draws on it, so nothing is said of either.
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
    fits_sentence: str | None = None
    budget_left: Figure | None = None
    headroom_left: Figure | None = None
    simulated: bool = False


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
        binding = _bound_by(room, need)
        shortfall, fits = _shortfall_and_fits(need, admits is not None, binding)
        name = None if binding is None else binding[0]
        return FundingAnswer(
            free, headroom, broker, need, admits=admits, binding=name, shortfall_cents=shortfall, fits=fits,
            sentence=_sentence(
                account_read=account is not None, own_found=own is not None, room=room,
                headroom_is_stale=headroom.state == "stale", need=need, admits=admits, binding=name,
                shortfall_cents=shortfall,
            ),
            action=_action(admits, name),
            fits_sentence=_fits_sentence(fits, name, refuses="this entry" if admits is False else None),
        )


def deploy_funding_answer(
    account: AccountBudget | None,
    *,
    amount_cents: int | None,
    need: EntryNeed | None,
    broker_buying_power: Figure | None,
    observed_at_ms: int,
    now_ms: int,
    simulated: bool = False,
) -> FundingAnswer:
    """Whether a budget of ``amount_cents`` can be set aside out of ``account`` and fund ``need`` (#3099).

    The answer for a bot that does not exist yet: the free budget is the
    amount typed, and the account headroom is the account's unreserved cash
    (there is no claim of the bot's own to add). ``amount_cents`` is ``None``
    before an amount is typed, which still shows the account's side.
    ``account`` is ``None`` when it could not be read; with nothing else
    known, only Alpaca's own limit can then be judged.

    A ``simulated`` budget is a Dry Run's: it takes money from no account, its
    private cash is the amount typed, and no account limit is said.

    Beyond the bot's rule, the budget is taken out of the account's cash, so
    the account must fund all of it: ``admits`` is also false when the budget
    is over the unreserved cash. ``observed_at_ms`` is when the reading behind
    ``account`` was taken and ``now_ms`` when this is asked.
    """
    if amount_cents is not None:
        _require_cents(amount_cents, "a budget")
    with money_context():
        free = _typed_budget(amount_cents, now_ms=now_ms)
        headroom = (
            _simulated_cash(amount_cents, now_ms=now_ms) if simulated
            else _unreserved_cash(account, observed_at_ms=observed_at_ms, now_ms=now_ms)
        )
        broker = _aged(broker_buying_power, now_ms=now_ms)
        room = _Room(_known_cents(free), _known_cents(headroom), _known_cents(broker))
        admits = _admits(room, need, set_aside=amount_cents)
        binding = _bound_by(room, need, set_aside=amount_cents)
        # An untyped budget is no missing reading: the account's own figures still say what fits.
        judged = admits is not None or (amount_cents is None and (account is None or room.headroom is not None))
        shortfall, fits = _shortfall_and_fits(need, judged, binding, set_aside=amount_cents)
        if account is None and not simulated:
            # Alpaca's own limit can be judged alone, but how many fit is the account's to say.
            fits = None
        name = None if binding is None else binding[0]
        return FundingAnswer(
            free, headroom, broker, need, admits=admits, binding=name, shortfall_cents=shortfall, fits=fits,
            sentence=_deploy_sentence(
                account_read=account is not None or simulated,
                room=_Room(room.free, None if simulated else room.headroom, room.broker),
                headroom_is_stale=headroom.state == "stale", need=need, admits=admits, binding=name,
                shortfall_cents=shortfall,
            ),
            action=_deploy_action(admits, name, need),
            fits_sentence=_fits_sentence(
                fits, name, simulated=simulated, refuses="this budget" if admits is False else None,
            ),
            budget_left=None if amount_cents is None else _left_after_one_entry(free, need, what="Budget typed in Deploy"),
            headroom_left=_left_after_one_entry(
                headroom, need, what="Simulated starting cash" if simulated else "Unreserved account cash",
            ),
            simulated=simulated,
        )


def option_buying_power_figure(options_buying_power: Decimal | None, *, read_at_ms: int) -> Figure:
    """Alpaca's option buying power as a figure that keeps the time it was read (#3099).

    A limit, so it rounds down to the cent. An answer that carried none is
    unknown and still keeps when Alpaca answered: never zero, never a pass.
    """
    if options_buying_power is None:
        return Figure(None, "unknown", f"{_ALPACA_ACCOUNT}, which did not carry option buying power", read_at_ms)
    cents = cents_spendable(options_buying_power)
    return Figure(cents, "known", _ALPACA_ACCOUNT, read_at_ms, (Term("Option buying power", cents),))


@dataclass(frozen=True, slots=True)
class RequirementEffect:
    """What one more entry does to the account's option requirement, beside what the bot sets aside for it (#3099).

    ``change`` is the account's requirement once the entry's contracts are
    held, less what it is now. ``netting`` is said only when that is less than
    the entry's own requirement: Alpaca values positions together and the bot
    does not spend the saving. The bot's own reservation is independent of
    every other bot's positions (ADR 0076 decision 28).
    """

    change: Figure
    netting: str | None


def requirement_effect(
    *, before_after_cents: tuple[int, int] | None, why_not: str, entry_requirement_cents: int, as_of_ms: int,
) -> RequirementEffect:
    """The account's requirement change for one entry, or why it cannot be worked out.

    ``before_after_cents`` is ``app.options.requirement.incremental_account_requirement``
    over the account's holdings and over them with the entry's contracts
    added; ``why_not`` is the reason it was not worked out, when it was not.
    ``entry_requirement_cents`` is the requirement of the entry's own legs,
    the part of its commitment that nets.
    """
    _require_cents(entry_requirement_cents, "an entry's requirement")
    if before_after_cents is None:
        return RequirementEffect(
            Figure(None, "unknown", f"{_HOLDINGS}, which could not be worked out: {why_not}", as_of_ms), None,
        )
    before, after = before_after_cents
    change = after - before
    figure = Figure(
        change, "known", _HOLDINGS, as_of_ms,
        (Term("Account requirement once this structure is held", after), Term("Account requirement now", -before)),
    )
    if change >= entry_requirement_cents:
        return RequirementEffect(figure, None)
    moved = (
        "would not change" if change == 0
        else f"would rise by only {_usd(change)}" if change > 0
        else f"would fall by {_usd(-change)}"
    )
    return RequirementEffect(
        figure,
        f"Alpaca values the account's positions together, so the account's requirement {moved}, though this "
        f"structure requires {_usd(entry_requirement_cents)} alone. The bot still sets aside "
        f"{_usd(entry_requirement_cents)}: it does not spend the {_usd(entry_requirement_cents - change)} "
        "that netting saves.",
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
    return Figure(amount, "known", _BOOKS, now_ms, terms_adding_to(amount, exact))


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
    return Figure(amount, "known" if fresh else "stale", _BOOKS_AND_READING, observed_at_ms, terms_adding_to(amount, exact))


def _typed_budget(amount_cents: int | None, *, now_ms: int) -> Figure:
    """The budget the owner typed at Deploy: a consent, never a reading, so it is as current as it is asked."""
    if amount_cents is None:
        return Figure.unknown(f"{_TYPED_BUDGET}, which has no amount yet")
    return Figure(amount_cents, "known", _TYPED_BUDGET, now_ms, (Term("Budget typed in Deploy", amount_cents),))


def _simulated_cash(amount_cents: int | None, *, now_ms: int) -> Figure:
    """A Dry Run's private cash: the amount typed, which no other bot and no order has a claim on."""
    if amount_cents is None:
        return Figure.unknown(f"{_SIMULATED}, which is the amount typed and has none yet")
    return Figure(amount_cents, "known", _SIMULATED, now_ms, (Term("Simulated starting cash", amount_cents),))


def _unreserved_cash(account: AccountBudget | None, *, observed_at_ms: int, now_ms: int) -> Figure:
    """The account's unreserved cash: what is left once every claim on it is taken off what Alpaca reported."""
    if account is None:
        return Figure.unknown(f"{_BOOKS_AND_READING}, which this review did not read")
    amount = cents_spendable(account.available)
    exact = [
        ("Cash in the account", account.cash),
        ("Set aside for running bots", -account.active_free_claims),
        ("Held in entry orders", -account.order_claims),
        ("Fees not posted yet", -account.fee_claims),
    ]
    if account.option_collateral:
        exact.append(("Collateral held against sold options", -account.option_collateral))
    fresh = reading_is_fresh(observed_at_ms, now_ms=now_ms, max_age_ms=OBSERVATION_MAX_AGE_MS)
    return Figure(amount, "known" if fresh else "stale", _BOOKS_AND_READING, observed_at_ms, terms_adding_to(amount, exact))


def _left_after_one_entry(limit: Figure, need: EntryNeed | None, *, what: str) -> Figure | None:
    """What ``limit`` leaves once one entry is taken from it, with its state and time: never a fresher amount."""
    if need is None:
        return None
    if limit.amount_cents is None:
        return Figure(
            None, "unknown", f"{limit.source}, so what it leaves after one entry is not known either", limit.as_of_ms,
        )
    return Figure(
        limit.amount_cents - need.commitment_cents, limit.state, limit.source, limit.as_of_ms,
        (Term(what, limit.amount_cents), Term("One entry", -need.commitment_cents)),
    )


def _aged(figure: Figure | None, *, now_ms: int) -> Figure | None:
    """A caller's figure under the same freshness limit as the account reading."""
    if figure is None or not figure.is_known or figure.as_of_ms is None:
        return figure
    if reading_is_fresh(figure.as_of_ms, now_ms=now_ms, max_age_ms=OBSERVATION_MAX_AGE_MS):
        return figure
    return replace(figure, state="stale")


def terms_adding_to(amount_cents: int, exact: Sequence[tuple[str, Decimal]]) -> tuple[Term, ...]:
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


def _refusing_limit(room: _Room, need: EntryNeed | None, *, set_aside: int | None = None) -> tuple[LimitName, int] | None:
    """The limit that refuses the entry and the room it has, or ``None`` when none does.

    Each limit is held to what it must cover: the budget the entry, the
    account the entry and, at a Deploy, the whole ``set_aside`` budget when
    that is more. Alpaca's option buying power never refuses. Where two do,
    the one short by more is named, and the budget before the account when
    they are short by the same, so a limit that covers what it must is never
    the one a refusal is said to be bound by.
    """
    required = 1 if need is None else need.commitment_cents
    account_required = required if set_aside is None else max(required, set_aside)
    short: list[tuple[LimitName, int, int]] = []
    if room.free is not None and room.free < required:
        short.append((_BOT_BUDGET, room.free, required - room.free))
    if room.headroom is not None and room.headroom < account_required:
        short.append((_ACCOUNT_HEADROOM, room.headroom, account_required - room.headroom))
    if not short:
        return None
    name, cents, _lacks = max(short, key=lambda item: item[2])
    return name, cents


def _bound_by(room: _Room, need: EntryNeed | None, *, set_aside: int | None = None) -> tuple[LimitName, int] | None:
    """The limit an answer is bound by: the one that refuses the entry when one does, else the one with the least room.

    Alpaca's own figure never refuses, so when our own limit does and the
    broker's has the least room, it is not the one named. A refusal is exactly
    what ``_admits`` reports as ``False``, so this needs no verdict handed in.
    """
    return _refusing_limit(room, need, set_aside=set_aside) or room.binding()


def _admits(room: _Room, need: EntryNeed | None, *, set_aside: int | None = None) -> bool | None:
    """The Clerk's rule on the bot's own two limits.

    With no need, an entry that could be priced still commits at least a cent
    (``app.options.requirement.entry_money`` refuses an entry whose
    requirement plus premium is zero or less, and a share costs a positive
    price), so a known limit with no room admits nothing, and anything else
    says nothing. A refusal a known limit makes stands whatever the other is;
    a pass needs both known. ``set_aside`` is the budget a Deploy takes out
    of the account's cash: the account must fund all of it, and at least one
    entry.
    """
    if _refusing_limit(room, need, set_aside=set_aside) is not None:
        return False
    if need is None or room.free is None or room.headroom is None:
        return None
    return True


def _shortfall_and_fits(
    need: EntryNeed | None, judged: bool, binding: tuple[LimitName, int] | None, *, set_aside: int | None = None,
) -> tuple[int | None, int | None]:
    """What the binding limit lacks and the whole entries it covers; ``None`` where that cannot be said.

    The account is short of what it must fund: the entry, or at a Deploy the
    whole ``set_aside`` budget when that is more.
    """
    if need is None or not judged or binding is None:
        return None, None
    name, room = binding
    required = need.commitment_cents
    if name == _ACCOUNT_HEADROOM and set_aside is not None:
        required = max(required, set_aside)
    fits = max(0, room) // need.commitment_cents if need.commitment_cents > 0 else None
    return max(0, required - room), fits


def _action(admits: bool | None, binding: LimitName | None) -> str | None:
    """What the owner can do, for the two limits that are theirs to move."""
    if admits is not False:
        return None
    if binding == _BOT_BUDGET:
        return _ACTION_BUDGET
    if binding == _ACCOUNT_HEADROOM:
        return _ACTION_HEADROOM
    return None


def _deploy_action(admits: bool | None, binding: LimitName | None, need: EntryNeed | None) -> str | None:
    """What the owner can do about a refused budget, for the two limits that are theirs to move."""
    if admits is not False:
        return None
    if binding == _BOT_BUDGET and need is not None:
        return f"Choose a budget of at least {_usd(need.commitment_cents)}."
    if binding == _ACCOUNT_HEADROOM:
        return _ACTION_DEPLOY_ACCOUNT
    return None


def _fits_sentence(
    fits: int | None, binding: LimitName | None, *, simulated: bool = False, refuses: str | None = None,
) -> str | None:
    """How many whole entries fit, said with the caveat that every bot draws on the same account (#3095, story 18).

    The count is of the limit that binds: the one with the least room for a
    pass, and for a refusal the limit that refuses, which need not have the
    least room; ``refuses`` is what it refuses, in words ("this budget" at a
    Deploy, "this entry" for a bot). A Dry Run's cash is its own, so it is
    counted against that cash and no caveat is said: no other bot draws on it.
    """
    if fits is None or binding is None:
        return None
    count = "No whole entry fits" if fits == 0 else "One whole entry fits" if fits == 1 else f"{fits:,} whole entries fit"
    if simulated:
        return f"{count} right now with this budget as its starting cash."
    which = f"the limit that refuses {refuses}" if refuses else "the limit with the least room"
    return (
        f"{count} right now under {_LIMIT_WORDS[binding]}, {which}. Every bot competes "
        "for the same account headroom, so these counts are never added up across bots."
    )


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
    else:
        limits = f"This bot {_has_free(free, only=True)}."
        if headroom < need.commitment_cents:
            can_fund = "nothing" if headroom < 0 else f"only {_usd(headroom)}"
            limits += f" The account can fund {can_fund} for it after protecting other positions."
    return f"{limits} {needs}{shortfall}"


def _unreserved_words(cents: int, *, only: bool = False) -> str:
    if cents < 0:
        return f"is {_usd(-cents)} over-committed, so nothing is unreserved"
    return f"has {'only ' if only else ''}{_usd(cents)} unreserved"


def _deploy_sentence(
    *, account_read: bool, room: _Room, headroom_is_stale: bool, need: EntryNeed | None,
    admits: bool | None, binding: LimitName | None, shortfall_cents: int | None,
) -> str:
    """The one sentence the owner reads at Deploy, about the budget typed (or none yet), the account and one entry.

    ``room.headroom`` is ``None`` for a Dry Run, whose account is no limit and is not said.
    """
    free, headroom, broker = room.free, room.headroom, room.broker
    if not account_read:
        # Nothing of the account was read, so only Alpaca's own figure can be told.
        if broker is None:
            return _DEPLOY_UNREAD
        power = f"Alpaca's option buying power is {_usd(broker)}"
        if need is None:
            return f"{power}."
        needs = f"the entry needs {_usd(need.commitment_cents)} including its fee provision"
        if shortfall_cents:
            return f"{power}, but {needs}. Shortfall: {_usd(shortfall_cents)}."
        return f"{power}, enough for the entry's {_usd(need.commitment_cents)} including its fee provision."
    stale = (
        f" The account's unreserved cash was last read more than {OBSERVATION_MAX_AGE_MS // 1000} seconds ago, "
        "so it is not counted here."
        if headroom_is_stale else ""
    )
    budget = "" if free is None else f"This budget is {_usd(free)}."
    account = "" if headroom is None else f"The account {_unreserved_words(headroom)}."
    context = " ".join(part for part in (budget, account) if part)
    if need is None:
        if admits is False and headroom is not None:
            if free is not None and headroom < free:
                return f"This budget is {_usd(free)}, but the account {_unreserved_words(headroom, only=True)}."
            return f"The account {_unreserved_words(headroom)}, and an entry always costs something."
        return f"{context}{stale}".strip() or _DEPLOY_UNREAD
    needs = f"The entry needs {_usd(need.commitment_cents)} including its fee provision."
    shortfall = f" Shortfall: {_usd(shortfall_cents)}." if shortfall_cents else ""
    broker_short = (
        f" Alpaca's option buying power is only {_usd(broker)}, so Alpaca would likely refuse the order.{shortfall}"
        if binding == _BROKER_BUYING_POWER and broker is not None and shortfall_cents else ""
    )
    if admits is True:
        assert free is not None, "a pass needs the budget known"
        enough = f"enough for the entry's {_usd(need.commitment_cents)} including its fee provision"
        lead = f"This budget is {_usd(free)}" if headroom is None else (
            f"This budget is {_usd(free)} and the account {_unreserved_words(headroom)}"
        )
        return f"{lead}, {enough}.{broker_short} {_PASS_NOTE}"
    if admits is None:
        unsaid = "" if headroom is not None else f"{stale} Whether the entry fits is not said."
        return f"{context}{unsaid} {needs}{broker_short}".strip()
    # Refused: by the budget or by the account, whichever the answer names; Alpaca's figure never refuses, but
    # it is said when it would not cover the entry either.
    also = (
        f" Alpaca's option buying power is only {_usd(broker)}, which would not cover the entry either."
        if broker is not None and broker < need.commitment_cents else ""
    )
    if binding == _ACCOUNT_HEADROOM and headroom is not None:
        if free is not None and free >= need.commitment_cents:
            return (
                f"This budget is {_usd(free)}, but the account {_unreserved_words(headroom, only=True)}."
                f"{shortfall}{also}{stale}"
            )
        return f"{budget} The account {_unreserved_words(headroom, only=True)}. {needs}{shortfall}{also}{stale}".strip()
    # The budget is what falls short.
    text = f"This budget is only {_usd(free)}. {needs}{shortfall}" if free is not None else f"{needs}{shortfall}"
    if headroom is not None and headroom < need.commitment_cents:
        text += f" The account {_unreserved_words(headroom, only=True)}."
    return text + also + stale
