"""What the account owns, what is spoken for, and where it is concentrated (#3101).

Four parts, each a ``funding.Figure`` that says where it came from, how old it
is and whether it is known, stale or unknown, in whole cents:

- **Owned**: the cash and the marked equity, as two figures, never one read as
  the other. On a Dry Run or a Live Shadow both are simulated and their source
  says so.
- **Spoken for**: a partition of the cash into the bots' free claims,
  collateral, entry orders, fees not posted yet, purchases from outside the
  system and what is left unreserved. What is the bots' opens by bot, stopped
  bots included; fees and outside purchases are the account's. A line that
  cannot be worked out makes the partition unknown; it is never left out to
  make the rest add up.
- **Concentration**: the collateral of each underlying and expiry taken alone,
  beside the account's own figure, which Alpaca takes as the largest expiry of
  each underlying.
- **Obligations and exit**: the assignment obligation by underlying, gross, and
  the cost to close every option position, the sum of the bots' exit estimates.

It informs and blocks nothing: no limit on combined exposure is added.
Figures are of one account and are never summed across accounts.

Formula (every amount whole cents):
  cash = free claims + collateral + entry orders + outside + fees + unreserved
  the budget's own identity (``budgets.account_budget``): ``available`` is
    the cash less every claim, so the lines add up to the cash exactly; each
    line is rounded half-even to the cent, the unreserved line floors as the
    Deploy preview's unreserved cash does, and what that leaves is one visible
    rounding line, never a hidden one;
  account collateral = ``held_requirement_cents`` of every bot's contracts
    netted together; collateral of one group = the same of the contracts of
    one underlying and expiry alone;
  assignment obligation(u) = Σ over sold contracts of u of |n| · strike · m;
  cost to close everything = Σ over bots of the bot's net close.
Reference: PRD #3095 part B and issue #3101; ADR 0076 decisions 22 and 28;
  Alpaca, "Options Level 3 Trading"
  (https://docs.alpaca.markets/docs/options-level-3-trading, as read for
  ``app.options.requirement`` on 2026-10-08): a position is valued by its
  largest expiry per underlying.
Canonical implementation: this file composes ``budgets.AccountBudget`` (the
  cash and every claim on it), ``options.requirement.held_requirement_cents``
  (collateral) and ``exit_estimate.ExitEstimate`` (the cost to close); none of
  their arithmetic is repeated here.
Validated against: tests/broker/alpaca/clerk/test_account_picture.py (every
  figure worked by hand).

The module reads no clock but the ``now_ms`` it is handed, no books and no
broker. It takes what the caller read.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.broker.alpaca.clerk.budgets import AccountBudget
from app.broker.alpaca.clerk.exit_estimate import BUY_BACK_ALONE_LINE, FEES_LINE, ExitEstimate
from app.broker.alpaca.clerk.funding import Figure, FigureState, Term, terms_adding_to
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS, reading_is_fresh
from app.broker.alpaca.clerk.money import ZERO, cents_spendable, display_cents, money_context
from app.options.contract import OptionContract
from app.options.event_allocation import assignment_cash_cents
from app.options.requirement import ContractAtMultiplier, held_requirement_cents, with_contracts

SpokenForKind = Literal["bot_free", "collateral", "orders", "outside", "fees", "unreserved", "rounding"]

_ALPACA = "Alpaca's account answer"
_BOOKS = "Clerk books"
_BOOKS_AND_READING = "Clerk books and the account reading"
_HOLDINGS = "The option contracts the books attribute to this account's bots, stopped bots included"

_CASH_TERM = "Cash as the account reports it"
_EQUITY_TERM = "Equity as the account reports it"

OWNED_NOTE = "Marked equity is cash plus positions at their marks. Only cash can be spent; equity never is."
EXPIRY_NOTE = (
    "Each row is one underlying and expiry taken alone. Alpaca takes only the largest expiry of each underlying, "
    "so the rows can add up to more than the account's collateral."
)
OBLIGATION_NOTE = (
    "The assignment obligation is the value, at the strikes, of the shares a sold option could oblige the account "
    "to buy or deliver. It is gross and is not a loss: a bounded loss is not a bounded obligation."
)
INFORMS_NOTE = "These figures inform. Nothing here blocks an entry or sets a limit."
PARTITION_ADDS_UP = "These lines add up to the account's cash, to the cent."
OVER_COMMITTED = " The claims on the account's cash are more than the cash."


@dataclass(frozen=True, slots=True)
class AccountReadings:
    """The account reading the picture's cash and equity came from.

    ``observed_at_ms`` is when it was taken and ``now_ms`` when the picture is
    asked; the reading is stale past ``OBSERVATION_MAX_AGE_MS``. ``equity`` is
    the reading's equity, or ``None`` when it carried none. ``simulated`` names
    the world whose cash and equity are its own simulation, and is ``None`` for
    a real account, whose figures are Alpaca's.
    """

    observed_at_ms: int
    now_ms: int
    equity: Decimal | None
    simulated: Literal["Dry Run", "Live Shadow"] | None = None


@dataclass(frozen=True, slots=True)
class Owned:
    broker_cash: Figure
    marked_equity: Figure
    note: str


@dataclass(frozen=True, slots=True)
class SpokenForLine:
    """One claim on the cash. Its figure's terms are what it is made of: the bots, by name, where it is theirs."""

    kind: SpokenForKind
    label: str
    figure: Figure


@dataclass(frozen=True, slots=True)
class SpokenFor:
    """The cash, partitioned. ``partition`` is the cash with the lines as its terms, or unknown with the reason."""

    partition: Figure
    lines: tuple[SpokenForLine, ...]
    sentence: str


@dataclass(frozen=True, slots=True)
class CollateralGroup:
    """The collateral of one underlying and expiry taken alone, in whole cents."""

    underlying: str
    expiry_ms: int
    collateral_cents: int


@dataclass(frozen=True, slots=True)
class Concentration:
    """Where the collateral is: each group alone, and the account's own figure, with terms by underlying."""

    account_collateral: Figure
    groups: tuple[CollateralGroup, ...]
    expiry_note: str | None


@dataclass(frozen=True, slots=True)
class Obligations:
    """What assignment could oblige, gross and apart from any loss, and what closing everything costs."""

    assignment: Figure
    assignment_note: str
    close_everything: Figure
    # Bots holding an option contract, and how many of them have an exit estimate that cannot be priced.
    bots_holding: int
    bots_unpriced: int
    conditions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AccountPicture:
    owned: Owned
    spoken_for: SpokenFor
    concentration: Concentration
    obligations: Obligations
    note: str


# ---- the two pieces of arithmetic ----------------------------------------------------------------------


def _require_held(symbol: str, multiplier: object, contracts: object) -> None:
    if isinstance(contracts, bool) or not isinstance(contracts, int):
        raise ValueError(f"{symbol} is held as {contracts!r}: option contracts are held in whole contracts")
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
        raise ValueError(f"the multiplier of {symbol} is a whole number, one or more; got {multiplier!r}")


def assignment_obligation(holdings: Mapping[ContractAtMultiplier, int]) -> dict[str, int]:
    """The gross value, in whole cents and by underlying, of the shares an assignment of the sold contracts could oblige.

    ``holdings`` is signed whole contracts by ``(OCC symbol, multiplier)``,
    bought above zero and sold below, as ``held_requirement_cents`` takes
    it. Only sold contracts oblige anything. Nothing is netted: a bought
    contract of the same underlying offsets no sold one here, premiums are
    left out, and what is shown is not a loss.

    Formula: obligation(u) = Σ over sold contracts c of u of |n_c| · K_c · m_c,
      with ``K`` the strike and ``m`` the contract's multiplier; a sold put
      struck at 665 is 1 · 665 · 100 = $66,500.00.
    Reference: ADR 0076 decision 22 (an assigned put buys, and an assigned call
      sells, multiplier × contracts shares at the strike, for strike ×
      multiplier × contracts in whole cents); Alpaca, "Options Trading"
      (https://docs.alpaca.markets/docs/options-trading), whose assignment
      this is the exposure to.
    Canonical implementation: the cash of one assignment is
      ``options.event_allocation.assignment_cash_cents``, called here for each
      sold contract and summed by underlying. The Options Backtest states the
      same figure in dollars
      (``options_backtest.cover_book.assignment_obligation``) from the same
      kernel; ``tests/broker/alpaca/clerk/test_account_picture.py`` holds the
      two to one answer over the same holdings.
    Validated against: tests/broker/alpaca/clerk/test_account_picture.py.

    Refuses as ``ValueError`` an amount that is not whole cents, a count that
    is not whole contracts, and a symbol that is not an option contract.
    Nothing is rounded.
    """
    obligation: dict[str, int] = defaultdict(int)
    for (symbol, multiplier), contracts in sorted(holdings.items()):
        _require_held(symbol, multiplier, contracts)
        if contracts >= 0:
            continue
        contract = OptionContract.from_occ_symbol(symbol)
        obligation[contract.root] += assignment_cash_cents(contract, contracts=-contracts, multiplier=multiplier)
    return dict(sorted(obligation.items()))


def collateral_by_underlying_and_expiry(holdings: Mapping[ContractAtMultiplier, int]) -> dict[tuple[str, int], int]:
    """The collateral of each underlying and expiry taken alone, in whole cents, keyed ``(root, expiry_ms)``.

    Each group is valued by the canonical requirement over its own contracts
    and nothing else. Alpaca takes the largest expiry of each underlying, so
    the groups of one underlying can add up to more than the account's
    collateral, and a group of bought contracts alone is worth zero.

    Formula: collateral(u, e) = ``held_requirement_cents`` of the contracts of
      underlying ``u`` expiring ``e``, calls and puts together.
    Reference: Alpaca, "Options Level 3 Trading"
      (https://docs.alpaca.markets/docs/options-level-3-trading, as read for
      ``app.options.requirement`` on 2026-10-08); ADR 0076 decision 28.
    Canonical implementation: this function over ``held_requirement_cents``;
      it adds no payoff arithmetic of its own.
    Validated against: tests/broker/alpaca/clerk/test_account_picture.py.

    Refuses what ``held_requirement_cents`` refuses.
    """
    return {group: held_requirement_cents(held) for group, held in _by_underlying_and_expiry(holdings).items()}


def _by_underlying_and_expiry(
    holdings: Mapping[ContractAtMultiplier, int],
) -> dict[tuple[str, int], dict[ContractAtMultiplier, int]]:
    groups: dict[tuple[str, int], dict[ContractAtMultiplier, int]] = defaultdict(dict)
    for key, contracts in sorted(holdings.items()):
        if contracts == 0:
            continue
        contract = OptionContract.from_occ_symbol(key[0])
        groups[contract.root, contract.expiry_ms][key] = contracts
    return dict(sorted(groups.items()))


# ---- the picture ---------------------------------------------------------------------------------------


def account_picture(
    account_budget: AccountBudget,
    readings: AccountReadings,
    holdings_by_bot: Mapping[str, Mapping[ContractAtMultiplier, int]] | None,
    exit_estimates: Mapping[str, ExitEstimate],
    *,
    holdings_unread: str = "",
) -> AccountPicture:
    """What the account owns, what is spoken for, where it is concentrated and what it is obliged to.

    ``account_budget`` is the account's cash and every claim on it, read once.
    ``holdings_by_bot`` is each bot's signed whole contracts by ``(OCC symbol,
    multiplier)``, stopped bots included, or ``None`` when the books could not
    be read, in which case ``holdings_unread`` says why and every figure that
    stands on them is unknown. ``exit_estimates`` is each holding bot's exit
    estimate, one whose figures are unknown for a bot that could not be priced;
    a holding bot with none at all is counted as unknown too.
    """
    running = frozenset(item.strategy_instance_id for item in account_budget.deployments if item.active)
    net: dict[ContractAtMultiplier, int] | None = None
    if holdings_by_bot is not None:
        net = {}
        for held in holdings_by_bot.values():
            net = with_contracts(net, held)
        net = {key: contracts for key, contracts in net.items() if contracts}
    return AccountPicture(
        owned=_owned(account_budget, readings),
        spoken_for=_spoken_for(account_budget, readings, running),
        concentration=_concentration(net, holdings_unread, now_ms=readings.now_ms),
        obligations=_obligations(
            net, holdings_by_bot, holdings_unread, exit_estimates, running=running, now_ms=readings.now_ms,
        ),
        note=INFORMS_NOTE,
    )


def _reading_state(readings: AccountReadings) -> FigureState:
    fresh = reading_is_fresh(readings.observed_at_ms, now_ms=readings.now_ms, max_age_ms=OBSERVATION_MAX_AGE_MS)
    return "known" if fresh else "stale"


def _owned(budget: AccountBudget, readings: AccountReadings) -> Owned:
    state = _reading_state(readings)
    at = readings.observed_at_ms
    if readings.simulated is None:
        cash_source = equity_source = _ALPACA
    else:
        cash_source = f"A {readings.simulated}'s simulated cash, which is not a figure of Alpaca's"
        equity_source = (
            f"A {readings.simulated}'s simulated equity: its cash and its positions at the marks it keeps, "
            "which is not a figure of Alpaca's"
        )
    cash_cents = display_cents(budget.cash)
    cash = Figure(cash_cents, state, cash_source, at, (Term(_CASH_TERM, cash_cents),))
    if readings.equity is None:
        equity = Figure(None, "unknown", f"{equity_source}, which this reading did not carry", at)
    else:
        equity_cents = display_cents(readings.equity)
        equity = Figure(equity_cents, state, equity_source, at, (Term(_EQUITY_TERM, equity_cents),))
    return Owned(cash, equity, OWNED_NOTE)


def _bot_label(sid: str, running: frozenset[str]) -> str:
    return f"Bot {sid}" if sid in running else f"Stopped bot {sid}"


def _line(
    kind: SpokenForKind, label: str, source: str, state: FigureState, as_of_ms: int,
    parts: Sequence[tuple[str, Decimal]],
) -> SpokenForLine:
    """A line worth the sum of its exact parts, rounded once, with each part rounded and any drift said."""
    present = [(name, amount) for name, amount in parts if amount != ZERO]
    cents = display_cents(sum((amount for _, amount in present), ZERO))
    return SpokenForLine(kind, label, Figure(cents, state, source, as_of_ms, terms_adding_to(cents, present)))


def _spoken_for(budget: AccountBudget, readings: AccountReadings, running: frozenset[str]) -> SpokenFor:
    with money_context():
        now, observed, state = readings.now_ms, readings.observed_at_ms, _reading_state(readings)
        free_claims = [
            (_bot_label(item.strategy_instance_id, running), item.cash_claim)
            for item in sorted(budget.deployments, key=lambda item: item.strategy_instance_id)
        ]
        collateral = [
            (_bot_label(item.strategy_instance_id, running), item.option_collateral)
            for item in sorted(budget.deployments, key=lambda item: item.strategy_instance_id)
        ]
        by_bot = [(_bot_label(sid, running), amount) for sid, amount in budget.bot_order_claims]
        # The order claims are one figure in ``available``; they are split here only when the split is whole.
        split_is_whole = budget.outside_claims + sum((amount for _, amount in by_bot), ZERO) == budget.order_claims
        lines = [
            _line("bot_free", "Set aside for running bots", f"{_BOOKS}: each running bot's budget less what it holds and has working",
                  "known", now, free_claims),
            _line("collateral", "Collateral held against sold options",
                  f"{_BOOKS}: the requirement of the contracts each bot holds, bot by bot, which the bots set aside "
                  "independently of one another",
                  "known", now, collateral),
        ]
        unsplit = f"{_BOOKS_AND_READING}, which cannot split the account's order claims by bot"
        if split_is_whole:
            lines += [
                _line("orders", "Held in entry orders",
                      f"{_BOOKS_AND_READING}: each entry order's unfilled cost and fee provision, and fills the "
                      "cash reading has not shown yet",
                      state, observed, by_bot),
                _line("outside", "Outside purchases not in the cash yet",
                      f"{_BOOKS_AND_READING}: purchases by manual trades and by orders placed outside the system "
                      "that the cash reading has not shown yet",
                      state, observed,
                      [("Manual trades and outside orders", budget.outside_claims)]),
            ]
        else:
            lines += [
                SpokenForLine("orders", "Held in entry orders", Figure.unknown(unsplit)),
                SpokenForLine("outside", "Outside purchases not in the cash yet", Figure.unknown(unsplit)),
            ]
        lines.append(_line(
            "fees", "Fees not posted yet",
            f"{_BOOKS_AND_READING}: fees the cash reading has not shown yet, which the books keep for the account "
            "as one figure",
            state, observed,
            [("Fees not posted yet", budget.fee_claims)],
        ))
        lines.append(_unreserved(budget, state=state, as_of_ms=observed, split_is_whole=split_is_whole))
        return _partition(budget, readings, tuple(lines))


def _unreserved(budget: AccountBudget, *, state: FigureState, as_of_ms: int, split_is_whole: bool) -> SpokenForLine:
    """What is left once every claim is taken off the cash: floored to the cent as the Deploy preview's is."""
    amount = cents_spendable(budget.available)
    orders = (
        [("Held in entry orders", -(budget.order_claims - budget.outside_claims)),
         ("Outside purchases not in the cash yet", -budget.outside_claims)]
        if split_is_whole else [("Held in entry orders and outside activity", -budget.order_claims)]
    )
    exact = [
        (_CASH_TERM, budget.cash),
        ("Set aside for running bots", -budget.active_free_claims),
        ("Collateral held against sold options", -budget.option_collateral),
        *orders,
        ("Fees not posted yet", -budget.fee_claims),
    ]
    terms = terms_adding_to(amount, [(name, value) for name, value in exact if value != ZERO])
    label = "Unreserved cash" if amount >= 0 else "Claims beyond the cash"
    return SpokenForLine(
        "unreserved", label,
        Figure(amount, state, f"{_BOOKS_AND_READING}: the cash less every claim above", as_of_ms, terms),
    )


def _partition(budget: AccountBudget, readings: AccountReadings, lines: tuple[SpokenForLine, ...]) -> SpokenFor:
    unknown = [line for line in lines if line.figure.state == "unknown"]
    if unknown:
        which = ", ".join(line.label.lower() for line in unknown)
        reason = f"{which} cannot be worked out"
        return SpokenFor(
            Figure.unknown(f"The account's cash cannot be partitioned: {reason}"),
            lines,
            f"The lines cannot be added up to the account's cash: {reason}.",
        )
    cash_cents = display_cents(budget.cash)
    drift = cash_cents - sum(line.figure.amount_cents or 0 for line in lines)
    # The cash and every line but the floored one are rounded half-even, each off by at most half a cent, and the
    # unreserved line floors, so it is off by less than a cent: together they can miss by no more than this many
    # whole cents. Anything beyond is a claim missing from the lines and never rounding, and is not a line to add.
    rounded = len(lines) - 1
    if abs(drift) > (rounded + 2) // 2:
        reason = "its lines miss the cash by more than rounding to whole cents can explain"
        return SpokenFor(
            Figure.unknown(f"The account's cash cannot be partitioned: {reason}"),
            lines,
            f"The lines cannot be added up to the account's cash: {reason}.",
        )
    if drift:
        lines = (
            *lines,
            SpokenForLine(
                "rounding", "Rounding to whole cents",
                Figure(drift, "known", "The cents each line was rounded to", readings.observed_at_ms,
                       (Term("Rounding to whole cents", drift),)),
            ),
        )
    stale = any(line.figure.state == "stale" for line in lines)
    partition = Figure(
        cash_cents, "stale" if stale else "known", f"{_BOOKS_AND_READING}, partitioned", readings.observed_at_ms,
        tuple(Term(line.label, line.figure.amount_cents or 0) for line in lines),
    )
    unreserved = next(line for line in lines if line.kind == "unreserved")
    over = (unreserved.figure.amount_cents or 0) < 0
    return SpokenFor(partition, lines, PARTITION_ADDS_UP + (OVER_COMMITTED if over else ""))


def _said(refusal: Exception | str) -> str:
    """A refusal's words as the end of a source, which is written without its full stop."""
    return str(refusal).rstrip(".")


def _concentration(net: Mapping[ContractAtMultiplier, int] | None, why: str, *, now_ms: int) -> Concentration:
    if net is None:
        return Concentration(Figure.unknown(f"{_HOLDINGS}, which could not be read: {_said(why)}"), (), None)
    try:
        collateral = collateral_by_underlying_and_expiry(net)
        # A contract belongs to exactly one underlying, so these subsets are disjoint and together are ``net``;
        # underlyings are valued apart, so their figures add to the account's own.
        by_underlying: dict[str, dict[ContractAtMultiplier, int]] = defaultdict(dict)
        for key, contracts in net.items():
            by_underlying[OptionContract.from_occ_symbol(key[0]).root][key] = contracts
        terms = tuple(Term(root, held_requirement_cents(held)) for root, held in sorted(by_underlying.items()))
        account = held_requirement_cents(net)
    except ValueError as refused:
        # ``StructureMoneyRefused`` is one: the holdings' loss has no limit, or an amount is not whole cents.
        return Concentration(Figure.unknown(f"{_HOLDINGS}, which could not be worked out: {_said(refused)}"), (), None)
    source = (
        "Alpaca's published rule over every bot's contracts together, the largest expiry of each underlying; "
        "the bots set aside their own collateral one by one, which can add up to more"
    )
    return Concentration(
        account_collateral=Figure(account, "known", source, now_ms, terms),
        groups=tuple(CollateralGroup(root, expiry_ms, cents) for (root, expiry_ms), cents in collateral.items()),
        expiry_note=EXPIRY_NOTE if collateral else None,
    )


def _obligations(
    net: Mapping[ContractAtMultiplier, int] | None,
    holdings_by_bot: Mapping[str, Mapping[ContractAtMultiplier, int]] | None,
    why: str,
    exit_estimates: Mapping[str, ExitEstimate],
    *, running: frozenset[str], now_ms: int,
) -> Obligations:
    unread = f"{_HOLDINGS}, which could not be read: {_said(why)}"
    if net is None or holdings_by_bot is None:
        return Obligations(
            assignment=Figure.unknown(unread), assignment_note=OBLIGATION_NOTE,
            close_everything=Figure.unknown(unread), bots_holding=0, bots_unpriced=0, conditions=(),
        )
    try:
        owed = assignment_obligation(net)
    except ValueError as refused:
        assignment = Figure.unknown(f"{_HOLDINGS}, which could not be worked out: {_said(refused)}")
    else:
        assignment = Figure(
            sum(owed.values()), "known",
            f"{_HOLDINGS}: each sold contract at its strike and multiplier, with nothing netted and no premium",
            now_ms, tuple(Term(root, cents) for root, cents in owed.items()),
        )
    holding = sorted(sid for sid, held in holdings_by_bot.items() if any(held.values()))
    close, unpriced = _close_everything(holding, exit_estimates, running=running, now_ms=now_ms)
    sold = any(contracts < 0 for sid in holding for contracts in holdings_by_bot[sid].values())
    return Obligations(
        assignment=assignment, assignment_note=OBLIGATION_NOTE, close_everything=close,
        bots_holding=len(holding), bots_unpriced=unpriced,
        conditions=(FEES_LINE, BUY_BACK_ALONE_LINE) if sold else (FEES_LINE,),
    )


def _close_everything(
    holding: Sequence[str], exit_estimates: Mapping[str, ExitEstimate], *, running: frozenset[str], now_ms: int,
) -> tuple[Figure, int]:
    """The sum of the bots' net closes, and how many bots' estimates cannot be priced.

    Unknown when any bot's is: a total that left a bot out would read as the
    cost of getting out of everything.
    """
    if not holding:
        return Figure(0, "known", "No bot holds an option contract, so there is nothing to close", now_ms), 0
    unpriced: list[str] = []
    closes: list[tuple[str, Figure]] = []
    for sid in holding:
        estimate = exit_estimates.get(sid)
        if estimate is None:
            unpriced.append(f"{_bot_label(sid, running)}: no exit estimate was read for it")
        elif estimate.net_close.state == "unknown":
            unpriced.append(f"{_bot_label(sid, running)}: {estimate.net_close.source}")
        else:
            closes.append((_bot_label(sid, running), estimate.net_close))
    if unpriced:
        return Figure.unknown(
            f"{len(unpriced)} of {len(holding)} bots' exit estimates cannot be priced, so the cost to close "
            f"everything is not known: {'; '.join(unpriced)}"
        ), len(unpriced)
    state: FigureState = "stale" if any(figure.state == "stale" for _, figure in closes) else "known"
    terms = tuple(Term(label, figure.amount_cents or 0) for label, figure in closes)
    oldest = min((figure.as_of_ms for _, figure in closes if figure.as_of_ms is not None), default=now_ms)
    return Figure(
        sum(term.amount_cents for term in terms), state,
        "The sum of the bots' exit estimates, each at the quotes held for its contracts", oldest, terms,
    ), 0


__all__ = [
    "EXPIRY_NOTE",
    "INFORMS_NOTE",
    "OBLIGATION_NOTE",
    "OWNED_NOTE",
    "PARTITION_ADDS_UP",
    "AccountPicture",
    "AccountReadings",
    "CollateralGroup",
    "Concentration",
    "Obligations",
    "Owned",
    "SpokenFor",
    "SpokenForLine",
    "account_picture",
    "assignment_obligation",
    "collateral_by_underlying_and_expiry",
]
