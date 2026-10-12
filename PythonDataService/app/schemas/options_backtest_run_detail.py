"""Wire shapes for reading a saved Options Backtest run closely: its leg trades, one of its sessions, and two runs side
by side (#3030).

The rules are the run service's (``options_backtest_runs.py``): snake_case; every instant is ``int64 ms UTC`` under
``MAX_TIMESTAMP_MS`` and a session is identified by its open, never by a date; money and prices are strings of exact
decimals and a name ending ``_usd`` is dollars; every model's name begins ``OptionsBacktest``; a closed set is an
inline ``Literal`` of its words and a test holds each to the enum it comes from.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.options.contract import OptionContract, OptionRight
from app.options_backtest.comparison import Comparison, RangeOfRun, RunTotals, SettingDifference, SharedSession
from app.options_backtest.results import Event, Fill, Mark
from app.options_backtest.run_detail import TradePage
from app.options_backtest.saved_runs import RunHeader, StoredTrade
from app.options_backtest.session_levels import ChartedFill, ChartedMark, Level
from app.options_backtest.session_series import SERIES_WORDS, Series
from app.options_backtest.session_view import SessionView
from app.schemas.options_backtest_runs import ExitReasonWord, Ms, SessionStatusWord, SkipReasonWord, exact

TRADES_PAGE_MAX = 5000
TRADES_PAGE_DEFAULT = 1000

EventKindWord = Literal[
    "picked",
    "pick_refused",
    "starting_price_taken",
    "no_starting_price",
    "momentum_hit",
    "entry_filled",
    "entry_dropped",
    "entry_refused_for_cash",
    "stop_hit",
    "squared_off",
    "exit_time",
    "close_filled",
    "re_entry_earned",
    "re_entry_blocked_by_cutoff",
    "re_entry_lost",
    "leg_cancelled",
    "marked_not_sold",
]
# The reasons an event gives: why a pick was refused, why an entry was refused, why a leg was cancelled.
EventReasonWord = Literal[
    "chain_incomplete",
    "expiry_not_listed",
    "expiry_not_a_session",
    "strike_not_listed",
    "not_standard",
    "cash_short",
    "square_off",
    "exit_time",
]
LevelKindWord = Literal["starting_price", "momentum", "stop"]
FillKindWord = Literal["entry", "exit"]
SeriesStateWord = Literal["shown", "not_shown"]
SeriesReasonWord = Literal[
    "session_skipped", "no_pin", "gone", "changed", "not_confirmed", "unreadable", "lake_unavailable"
]
SettingWord = Literal[
    "entry_time",
    "exit_time",
    "no_reentry_after",
    "square_off",
    "leg",
    "leg_right",
    "leg_contracts",
    "leg_expiry",
    "leg_strike",
    "leg_stop_loss",
    "leg_momentum",
    "leg_reentry",
    "slippage",
    "price_step",
    "fees",
    "starting_cash",
]


class OptionsBacktestContractResponse(BaseModel):
    """An option contract: its OCC symbol, and the symbol read out for display."""

    symbol: str
    underlying: str
    right: OptionRight
    strike_usd: str
    # The expiry's 16:00 New York anchor. It names a date: show it as a date in New York time, never in the viewer's.
    expiry_ms: Ms

    @classmethod
    def from_contract(cls, contract: OptionContract) -> Self:
        return cls(
            symbol=contract.occ_symbol,
            underlying=contract.root,
            right=contract.right,
            strike_usd=exact(contract.strike),
            expiry_ms=contract.expiry_ms,
        )


# ── Leg trades ───────────────────────────────────────────────────────────


class OptionsBacktestTradeFillResponse(BaseModel):
    """An order that filled: the bar it filled in, the open of that bar (the price seen) and the price it filled at."""

    bar_close_ms: Ms
    seen_usd: str
    filled_usd: str


class OptionsBacktestTradeMarkResponse(BaseModel):
    """A leg valued and not sold: the last bar its contract traded in and the close of that bar."""

    bar_close_ms: Ms
    price_usd: str


class OptionsBacktestTradeResponse(BaseModel):
    """One leg round trip. It left by a ``sale`` or, when its sale never filled, by a ``mark``; exactly one is set."""

    session_open_ms: Ms
    # Its place in the session's order, from 0, as the run recorded it.
    trade_number: int = Field(ge=0)
    leg_id: str
    # 0 for the leg's first entry, 1 for its first re-entry, and so on.
    entry_number: int = Field(ge=0)
    contract: OptionsBacktestContractResponse
    contracts: int
    multiplier: int
    # Set when the leg waited on momentum.
    starting_price_usd: str | None = None
    momentum_level_usd: str | None = None
    # Set when the leg carried a stop loss.
    stop_level_usd: str | None = None
    entry_decided_at_ms: Ms
    entry: OptionsBacktestTradeFillResponse
    exit_reason: ExitReasonWord
    exit_group: int
    exit_decided_at_ms: Ms
    exit_kind: Literal["sale", "mark"]
    sale: OptionsBacktestTradeFillResponse | None = None
    mark: OptionsBacktestTradeMarkResponse | None = None
    entry_fee_usd: str
    # Zero for a leg that was marked and not sold.
    exit_fee_usd: str
    pnl_usd: str

    @model_validator(mode="after")
    def _the_exit_is_the_one_it_says(self) -> Self:
        if (self.exit_kind == "sale") != (self.sale is not None) or (self.exit_kind == "mark") != (
            self.mark is not None
        ):
            raise ValueError("a trade left by a sale or by a mark, and carries exactly the one its exit_kind names")
        return self

    @classmethod
    def from_stored(cls, stored: StoredTrade) -> Self:
        trade, leaving = stored.trade, stored.trade.exit
        entry = trade.entry
        sold = isinstance(leaving, Fill)
        return cls(
            session_open_ms=stored.session_open_ms,
            trade_number=stored.trade_number,
            leg_id=trade.leg_id,
            entry_number=trade.entry_number,
            contract=OptionsBacktestContractResponse.from_contract(trade.contract),
            contracts=trade.contracts,
            multiplier=trade.multiplier,
            starting_price_usd=None if trade.starting_price is None else exact(trade.starting_price),
            momentum_level_usd=None if trade.momentum_level is None else exact(trade.momentum_level),
            stop_level_usd=None if trade.stop_level is None else exact(trade.stop_level),
            entry_decided_at_ms=trade.entry_decided_at_ms,
            entry=OptionsBacktestTradeFillResponse(
                bar_close_ms=entry.bar_close_ms, seen_usd=exact(entry.seen), filled_usd=exact(entry.filled)
            ),
            exit_reason=trade.exit_reason.value,  # type: ignore[arg-type]
            exit_group=trade.exit_group,
            exit_decided_at_ms=trade.exit_decided_at_ms,
            exit_kind="sale" if sold else "mark",
            sale=OptionsBacktestTradeFillResponse(
                bar_close_ms=leaving.bar_close_ms, seen_usd=exact(leaving.seen), filled_usd=exact(leaving.filled)
            )
            if isinstance(leaving, Fill)
            else None,
            mark=OptionsBacktestTradeMarkResponse(bar_close_ms=leaving.bar_close_ms, price_usd=exact(leaving.price))
            if isinstance(leaving, Mark)
            else None,
            entry_fee_usd=exact(trade.entry_fee_usd),
            exit_fee_usd=exact(trade.exit_fee_usd),
            pnl_usd=exact(trade.pnl_usd),
        )


class OptionsBacktestTradesResponse(BaseModel):
    """A page of a run's leg trades. ``total`` counts every trade that matches, on every page; ``next_offset`` is the
    ``offset`` to ask for the rest, and is ``None`` on the last page. The CSV export is never paged.

    A ``leg_id`` that the run's setup does not have is a 404 when the setup can be read. On a run whose stored setup can
    no longer be read there is no setup to say which legs the run had, so an unknown ``leg_id`` is answered from the
    stored trades alone: a 200 with ``total`` 0, which says only that no trade was stored for that leg."""

    run_id: int
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=TRADES_PAGE_MAX)
    next_offset: int | None = None
    trades: list[OptionsBacktestTradeResponse]

    @classmethod
    def from_page(cls, page: TradePage) -> Self:
        return cls(
            run_id=page.run_id,
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            next_offset=page.next_offset,
            trades=[OptionsBacktestTradeResponse.from_stored(stored) for stored in page.trades],
        )


# ── One session, for the one-day timeline ────────────────────────────────


class OptionsBacktestEventResponse(BaseModel):
    """One thing that happened in the session, in the order it happened.

    ``entry_number`` follows the replay's one rule: an event about a position carries that position's number, and an
    event about an attempt to enter carries the number that entry has or would have. ``detail`` is the one place the
    replay writes prose (a refused pick says why). It is the backend's words for the owner, stored and served exactly
    as written, even when they name a date; nothing else on the wire names one, since a session is its open.
    """

    kind: EventKindWord
    at_ms: Ms
    leg_id: str
    entry_number: int = Field(ge=0)
    # The OCC symbol of the contract the event is about, when it is about one.
    contract: str | None = None
    price_usd: str | None = None
    reason: EventReasonWord | None = None
    detail: str | None = None

    @classmethod
    def from_event(cls, event: Event) -> Self:
        return cls(
            kind=event.kind.value,  # type: ignore[arg-type]
            at_ms=event.at_ms,
            leg_id=event.leg_id,
            entry_number=event.entry_number,
            contract=None if event.contract is None else event.contract.occ_symbol,
            price_usd=None if event.price is None else exact(event.price),
            reason=event.reason,  # type: ignore[arg-type]
            detail=event.detail,
        )


class OptionsBacktestSeriesPointResponse(BaseModel):
    """A bar's close time and close price. A minute with no trade has no point."""

    at_ms: Ms
    close_usd: str


class OptionsBacktestSeriesResponse(BaseModel):
    """One instrument's minute closes across the session, or why they are not shown.

    A series is shown only if the file it is read from is the one the run read (its sha256 is the stored pin). Otherwise
    ``reason`` says why and ``message`` says it in words, and there are no points: a saved run is never drawn from
    changed data. ``session_skipped``: the session was skipped on its window, so no bars were read for it; ``no_pin``:
    the run kept no record of reading this one; ``gone``: no longer in the lake; ``changed``: not the file the run read;
    ``not_confirmed``: the lake's catalog does not admit the file's bytes, so they cannot be checked against what the run
    read (nobody saw them change); ``unreadable``: the file is there and cannot be read (damaged, a bar no session can
    place, or refused by the operating system); ``lake_unavailable``: the lake could not be asked, which says nothing of
    the file.
    """

    # The underlying's ticker, or a contract's OCC symbol.
    symbol: str
    # The contract read out for display; ``None`` for the underlying.
    contract: OptionsBacktestContractResponse | None = None
    state: SeriesStateWord
    reason: SeriesReasonWord | None = None
    message: str | None = None
    points: list[OptionsBacktestSeriesPointResponse]

    @model_validator(mode="after")
    def _shown_or_why_not(self) -> Self:
        if self.state == "shown":
            consistent = self.reason is None and self.message is None
        else:
            consistent = self.reason is not None and self.message is not None and not self.points
        if not consistent:
            raise ValueError("a series is shown, or it has a reason and a message and no points")
        return self

    @classmethod
    def from_series(cls, series: Series) -> Self:
        shown = series.reason is None
        return cls(
            symbol=series.symbol,
            contract=None
            if series.contract is None
            else OptionsBacktestContractResponse.from_contract(series.contract),
            state="shown" if shown else "not_shown",
            reason=None if series.reason is None else series.reason.value,  # type: ignore[arg-type]
            message=None if series.reason is None else SERIES_WORDS[series.reason],
            points=[OptionsBacktestSeriesPointResponse(at_ms=p.at_ms, close_usd=exact(p.close)) for p in series.points],
        )


class OptionsBacktestLevelResponse(BaseModel):
    """A horizontal line to draw at one price, from ``from_ms`` to ``to_ms`` (both minute closes).

    One rule ends a line that waits: it ends when the thing it waits for happened, whatever came next. ``starting_price``
    and ``momentum`` belong to a leg waiting on momentum and run from the minute its starting price was taken to the
    minute the buy was decided (the minute whose close reached the momentum level), whether the buy then filled or was
    refused for cash; a leg cancelled before it reached the level waited until it was cancelled. ``stop`` belongs to a
    position and runs from the minute it filled to the minute the close that sold it was decided.
    """

    kind: LevelKindWord
    leg_id: str
    entry_number: int = Field(ge=0)
    contract: str
    price_usd: str
    from_ms: Ms
    to_ms: Ms

    @classmethod
    def from_level(cls, level: Level) -> Self:
        return cls(
            kind=level.kind.value,  # type: ignore[arg-type]
            leg_id=level.leg_id,
            entry_number=level.entry_number,
            contract=level.contract.occ_symbol,
            price_usd=exact(level.price),
            from_ms=level.from_ms,
            to_ms=level.to_ms,
        )


class OptionsBacktestFillResponse(BaseModel):
    """An order that filled, to mark on the bar it filled in. ``seen_usd`` is that bar's open and ``filled_usd`` what the
    order filled at after slippage and the price step. ``exit_reason`` is why a sale was made; ``null`` for an entry."""

    kind: FillKindWord
    leg_id: str
    entry_number: int = Field(ge=0)
    contract: str
    contracts: int
    bar_close_ms: Ms
    seen_usd: str
    filled_usd: str
    exit_reason: ExitReasonWord | None = None

    @classmethod
    def from_fill(cls, fill: ChartedFill) -> Self:
        return cls(
            kind=fill.kind,
            leg_id=fill.leg_id,
            entry_number=fill.entry_number,
            contract=fill.contract.occ_symbol,
            contracts=fill.contracts,
            bar_close_ms=fill.bar_close_ms,
            seen_usd=exact(fill.seen),
            filled_usd=exact(fill.filled),
            exit_reason=None if fill.exit_reason is None else fill.exit_reason.value,  # type: ignore[arg-type]
        )


class OptionsBacktestMarkResponse(BaseModel):
    """A leg valued and not sold: the last bar its contract traded in and that bar's close. It is not a fill."""

    leg_id: str
    entry_number: int = Field(ge=0)
    contract: str
    bar_close_ms: Ms
    price_usd: str

    @classmethod
    def from_mark(cls, mark: ChartedMark) -> Self:
        return cls(
            leg_id=mark.leg_id,
            entry_number=mark.entry_number,
            contract=mark.contract.occ_symbol,
            bar_close_ms=mark.bar_close_ms,
            price_usd=exact(mark.price),
        )


class OptionsBacktestSessionResponse(BaseModel):
    """One session of a complete run: what was stored, and what a timeline draws.

    The stored facts (status, cash, profit, ``trades``, ``events``, ``levels``, ``fills``, ``marks``) never depend on the
    lake. The series are read from it and are checked against what the run read; one that cannot be shown says so and
    the rest is still served.

    ``setup_cannot_open_reason`` is set only for a run whose stored setup no longer validates, and says why. Everything
    stored is served all the same; the one thing missing is the ``momentum`` line of an attempt that never filled,
    whose level comes from the leg's rule.
    """

    run_id: int
    session_open_ms: Ms
    session_close_ms: Ms
    status: SessionStatusWord
    skip_reason: SkipReasonWord | None = None
    cash_before_usd: str
    cash_after_usd: str
    profit_usd: str
    trades: list[OptionsBacktestTradeResponse]
    # In the order they happened.
    events: list[OptionsBacktestEventResponse]
    underlying: OptionsBacktestSeriesResponse
    # The contracts the session traded or took a starting price in, by OCC symbol.
    contracts: list[OptionsBacktestSeriesResponse]
    levels: list[OptionsBacktestLevelResponse]
    fills: list[OptionsBacktestFillResponse]
    marks: list[OptionsBacktestMarkResponse]
    setup_cannot_open_reason: str | None = Field(
        default=None,
        description=(
            "Why the run's stored setup can no longer be read; null when it can. When it is set, `levels` leaves out "
            "the `momentum` line of every attempt that never filled, because that line is the leg's momentum rule "
            "applied to the attempt's starting price and the rule is lost. Nothing else is left out: the same "
            "attempt's `starting_price` line, the lines of every attempt that filled, and the trades, events, fills, "
            "marks and series are served as for any run."
        ),
    )

    @classmethod
    def from_view(cls, view: SessionView) -> Self:
        record = view.record
        stored = record.session
        session = stored.result
        outcome = session.outcome
        return cls(
            run_id=record.header.record.id,
            session_open_ms=session.session_open_ms,
            session_close_ms=session.session_close_ms,
            status=outcome.status.value,  # type: ignore[arg-type]
            skip_reason=None if outcome.skip_reason is None else outcome.skip_reason.value,  # type: ignore[arg-type]
            cash_before_usd=exact(session.cash_before_usd),
            cash_after_usd=exact(session.cash_after_usd),
            profit_usd=exact(stored.profit_usd),
            # ``complete_run`` numbers a session's trades from 0 in the order it stores them, so the place is the number.
            trades=[
                OptionsBacktestTradeResponse.from_stored(StoredTrade(session.session_open_ms, number, trade))
                for number, trade in enumerate(outcome.trades)
            ],
            events=[OptionsBacktestEventResponse.from_event(event) for event in outcome.events],
            underlying=OptionsBacktestSeriesResponse.from_series(view.series.underlying),
            contracts=[OptionsBacktestSeriesResponse.from_series(each) for each in view.series.contracts],
            levels=[OptionsBacktestLevelResponse.from_level(level) for level in view.levels],
            fills=[OptionsBacktestFillResponse.from_fill(fill) for fill in view.fills],
            marks=[OptionsBacktestMarkResponse.from_mark(mark) for mark in view.marks],
            setup_cannot_open_reason=record.header.setup_cannot_open_reason,
        )


# ── Two runs side by side ────────────────────────────────────────────────


class OptionsBacktestComparedRunResponse(BaseModel):
    """One of the two runs, as the history shows it."""

    id: int
    setup_name: str | None = None
    created_at_ms: Ms
    first_session_open_ms: Ms
    last_session_open_ms: Ms
    sessions_requested: int
    # The fee policy in a sentence, written when the run was made.
    fee_policy: str

    @classmethod
    def from_header(cls, header: RunHeader) -> Self:
        record = header.record
        return cls(
            id=record.id,
            setup_name=record.setup_name,
            created_at_ms=record.created_at_ms,
            first_session_open_ms=record.first_session_open_ms,
            last_session_open_ms=record.last_session_open_ms,
            sessions_requested=record.sessions_requested,
            fee_policy=record.fee_policy_text,
        )


class OptionsBacktestSettingDifferenceResponse(BaseModel):
    """One setting on which the two runs differ. ``label`` names it the way an owner would ("leg call: re-entry on stop");
    ``a`` and ``b`` are each run's value in words ("not set" for a rule that is off, "not in this run" for a leg the run
    lacks). The settings were compared as validated objects, so ``0.3`` and ``0.3000`` are never a difference."""

    setting: SettingWord
    # The leg a setting belongs to; ``null`` for a setting of the whole run.
    leg_id: str | None = None
    label: str
    a: str
    b: str

    @classmethod
    def from_difference(cls, difference: SettingDifference) -> Self:
        return cls(
            setting=difference.setting.value,  # type: ignore[arg-type]
            leg_id=difference.leg_id,
            label=difference.label,
            a=difference.a,
            b=difference.b,
        )


class OptionsBacktestRangeResponse(BaseModel):
    first_session_open_ms: Ms
    last_session_open_ms: Ms
    sessions_requested: int

    @classmethod
    def from_range(cls, each: RangeOfRun) -> Self:
        return cls(
            first_session_open_ms=each.first_session_open_ms,
            last_session_open_ms=each.last_session_open_ms,
            sessions_requested=each.sessions_requested,
        )


class OptionsBacktestRangeDifferenceResponse(BaseModel):
    """The two runs' ranges, sent only when they differ."""

    a: OptionsBacktestRangeResponse
    b: OptionsBacktestRangeResponse


class OptionsBacktestRunTotalsResponse(BaseModel):
    """A run's figures over the sessions both runs tested, and only those. Sums of the stored per-session figures."""

    net_profit_usd: str
    trades: int
    # Shared sessions whose profit is above zero.
    winning_sessions: int

    @classmethod
    def from_totals(cls, totals: RunTotals) -> Self:
        return cls(
            net_profit_usd=exact(totals.net_profit_usd),
            trades=totals.trades,
            winning_sessions=totals.winning_sessions,
        )


class OptionsBacktestSharedSessionResponse(BaseModel):
    """A session both runs tested, with each run's status and profit, so a page can show where one setting helped or hurt."""

    session_open_ms: Ms
    a_status: SessionStatusWord
    b_status: SessionStatusWord
    a_profit_usd: str
    b_profit_usd: str

    @classmethod
    def from_shared(cls, shared: SharedSession) -> Self:
        return cls(
            session_open_ms=shared.session_open_ms,
            a_status=shared.a.status.value,  # type: ignore[arg-type]
            b_status=shared.b.status.value,  # type: ignore[arg-type]
            a_profit_usd=exact(shared.a.profit_usd),
            b_profit_usd=exact(shared.b.profit_usd),
        )


class OptionsBacktestSessionOverlapResponse(BaseModel):
    """The sessions the two runs share (by their opens), how many each has that the other lacks, and the totals over the
    shared ones."""

    shared_sessions: int
    only_in_a: int
    only_in_b: int
    a: OptionsBacktestRunTotalsResponse
    b: OptionsBacktestRunTotalsResponse
    # One per shared session, in order.
    sessions: list[OptionsBacktestSharedSessionResponse]


class OptionsBacktestComparisonResponse(BaseModel):
    """Two complete runs of one underlying compared. Python works out everything here; the page only shows it."""

    underlying: str
    a: OptionsBacktestComparedRunResponse
    b: OptionsBacktestComparedRunResponse
    # Every setting that differs, in a fixed order; empty when the two runs ran the same setup.
    setting_differences: list[OptionsBacktestSettingDifferenceResponse]
    # Set only when the two runs' ranges differ.
    range_difference: OptionsBacktestRangeDifferenceResponse | None = None
    overlap: OptionsBacktestSessionOverlapResponse

    @classmethod
    def from_comparison(cls, a: RunHeader, b: RunHeader, comparison: Comparison) -> Self:
        sessions = comparison.sessions
        return cls(
            underlying=comparison.underlying,
            a=OptionsBacktestComparedRunResponse.from_header(a),
            b=OptionsBacktestComparedRunResponse.from_header(b),
            setting_differences=[
                OptionsBacktestSettingDifferenceResponse.from_difference(each) for each in comparison.settings
            ],
            range_difference=OptionsBacktestRangeDifferenceResponse(
                a=OptionsBacktestRangeResponse.from_range(comparison.range_a),
                b=OptionsBacktestRangeResponse.from_range(comparison.range_b),
            )
            if comparison.ranges_differ
            else None,
            overlap=OptionsBacktestSessionOverlapResponse(
                shared_sessions=len(sessions.shared),
                only_in_a=sessions.only_in_a,
                only_in_b=sessions.only_in_b,
                a=OptionsBacktestRunTotalsResponse.from_totals(sessions.a),
                b=OptionsBacktestRunTotalsResponse.from_totals(sessions.b),
                sessions=[OptionsBacktestSharedSessionResponse.from_shared(each) for each in sessions.shared],
            ),
        )
