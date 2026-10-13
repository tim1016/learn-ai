"""Wire shapes for the Options Backtest page's runs: Check data, the history, one run, and the job that makes one (#3030).

Names are snake_case and every model's name begins ``OptionsBacktest``, so no served model shares a name with another
and the published contract keeps each one's own. Every instant is ``int64 ms UTC`` under the repo's ceiling
(``MAX_TIMESTAMP_MS``); a trading date is the open of its session, and a date the owner picks (the first date option
prices are available from) is New York midnight of it. Money and prices are strings of exact decimals and a name ending
``_usd`` is dollars; a percent is a string and is named for it. A rate is its numerator, its denominator and the quotient
rounded to six places, the one rounded figure, named as rounded: ``rounded_to_6_places``. A closed set is written as a
``Literal`` of its words here, and a test holds each to the enum it comes from.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options_backtest.capital_evidence import CapitalEvidence, CapitalInvalidEvent
from app.options_backtest.cash_ladder import CashLevel
from app.options_backtest.data_check import DataCheck, Problem, SessionCheck, SessionVerdict, TestableStretch
from app.options_backtest.run_stamps import RunStamps
from app.options_backtest.saved_runs import FailureItem, Headline, RunFailure, RunRecord, StoredRun
from app.options_backtest.saved_setups import SETUP_NAME_PATTERN
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_backtest.statistics import DayRow, Drawdown, MonthRow, Ratio, RunSummary
from app.schemas.options_backtest import served_setup
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_day_end_ms, et_midnight_ms

Ms = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]
# A run's id is the database's; it stays within what a browser holds exactly.
RUN_ID_MAX = 2**53 - 1
# The id the jobs boundary mints for a job is a GUID (36 characters of hex and dashes). A job id, in a request body or
# in the history's filter, is 1 to 128 of letters, digits, "-" and "_": nothing a database or a Redis key can refuse.
JOB_ID_MAX_LENGTH = 128
JOB_ID_PATTERN = r"^[A-Za-z0-9_-]+$"

# What a range's two instants are, as the wire says it: a range that sends any other is a 400 with this code.
START_MS_RULE = (
    "New York midnight of the range's first day. Any other instant is refused with a 400 and the code "
    "OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT."
)
END_MS_RULE = (
    "New York midnight of the day after the range's last day. Any other instant is refused with a 400 and the code "
    "OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT."
)

# What a run's setup_name is: the run is kept under it only when it is true, and a name that is not is no refusal.
RUN_SETUP_NAME_RULE = (
    "The name the setup was saved under, when the run was started from one. The run is recorded under it only when a "
    "saved setup or a shipped example of that name exists and is equal to the setup sent; otherwise the run is "
    "recorded with no name, and is accepted all the same."
)

SessionStatusWord = Literal["traded", "no_trade", "skipped", "incomplete"]
SkipReasonWord = Literal["window_does_not_fit", "underlying_did_not_trade"]
ExitReasonWord = Literal["stop", "square_off", "exit_time", "marked_not_sold", "marked_not_bought_back"]
PickRefusalWord = Literal[
    "chain_incomplete", "expiry_not_listed", "expiry_not_a_session", "strike_not_listed", "not_standard"
]
CapitalInvalidKindWord = Literal["unfunded_close", "position_marked"]
RunStatusWord = Literal["running", "complete", "failed", "cancelled", "interrupted"]
VerdictWord = Literal["ready", "needs_download", "cannot_be_tested"]
ProblemCodeWord = Literal[
    "older_than_the_data_plan",
    "session_data_not_final",
    "no_published_fee_rates",
    "lake_holds_a_failure",
    "stored_data_is_malformed",
]
MissingWord = Literal["underlying_bars", "listing", "contracts"]


def exact(value: Decimal) -> str:
    """A decimal as plain digits, never in exponent form, so the text names the same number to any reader."""
    return format(value, "f")


def _exact_or_none(value: Decimal | None) -> str | None:
    return None if value is None else exact(value)


# ── Check data ───────────────────────────────────────────────────────────


class OptionsBacktestCheckDataRequest(BaseModel):
    """A setup and the half-open range ``[start_ms, end_ms)`` to check: New York midnight of the first date to the
    New York midnight after the last, as Grid Search's window is."""

    model_config = ConfigDict(extra="forbid")

    setup: OptionsBacktestSetup
    start_ms: Ms = Field(description=START_MS_RULE)
    end_ms: Ms = Field(description=END_MS_RULE)


class OptionsBacktestProblemResponse(BaseModel):
    code: ProblemCodeWord
    message: str
    # The lake's own reason code, set when the problem is a failure the lake recorded.
    lake_reason: str | None = None

    @classmethod
    def from_problem(cls, problem: Problem) -> Self:
        return cls(code=problem.code.value, message=problem.message, lake_reason=problem.lake_reason)  # type: ignore[arg-type]


class OptionsBacktestContractNeedsResponse(BaseModel):
    """How many contract days a session needs and how many are not stored. ``not_yet_known`` while the session's
    underlying day or listing is not stored, and both counts are then ``None``."""

    state: Literal["known", "not_yet_known"]
    needed: int | None = None
    missing: int | None = None


class OptionsBacktestSessionCheckResponse(BaseModel):
    session_open_ms: Ms
    session_close_ms: Ms
    verdict: VerdictWord
    # Set on a ready session that the replay will skip, and says which rule skips it.
    skip: SkipReasonWord | None = None
    # What a download must bring; empty unless the verdict is ``needs_download``.
    missing: list[MissingWord]
    contracts: OptionsBacktestContractNeedsResponse
    # Set exactly when the verdict is ``cannot_be_tested``.
    problems: list[OptionsBacktestProblemResponse]
    # Plain words about anything else worth knowing: a damaged stored file, an earlier try that failed.
    notes: list[str]

    @classmethod
    def from_session(cls, session: SessionCheck) -> Self:
        needs = session.contracts
        return cls(
            session_open_ms=session.session_open_ms,
            session_close_ms=session.session_close_ms,
            verdict=session.verdict.value,  # type: ignore[arg-type]
            skip=None if session.skip is None else session.skip.value,  # type: ignore[arg-type]
            missing=[kind.value for kind in session.missing],  # type: ignore[misc]
            contracts=OptionsBacktestContractNeedsResponse(
                state="known" if needs.known else "not_yet_known", needed=needs.needed, missing=needs.missing
            ),
            problems=[OptionsBacktestProblemResponse.from_problem(problem) for problem in session.problems],
            notes=list(session.notes),
        )


class OptionsBacktestTestableStretchResponse(BaseModel):
    """A run of consecutive sessions that can all be tested, with the range to ask for to run just them."""

    start_ms: Ms
    end_ms: Ms
    first_session_open_ms: Ms
    last_session_open_ms: Ms
    sessions: int

    @classmethod
    def from_stretch(cls, stretch: TestableStretch) -> Self:
        return cls(
            start_ms=et_midnight_ms(stretch.first),
            end_ms=et_day_end_ms(stretch.last),
            first_session_open_ms=stretch.first_session_open_ms,
            last_session_open_ms=stretch.last_session_open_ms,
            sessions=stretch.sessions,
        )


class OptionsBacktestDataCountsResponse(BaseModel):
    ready: int
    needs_download: int
    cannot_be_tested: int


class OptionsBacktestDataCheckResponse(BaseModel):
    """Which sessions of a range can be tested, from what is already stored. Nothing was fetched to answer."""

    # New York midnight of the first date option prices are available for on this plan.
    option_prices_from_ms: Ms
    sessions: list[OptionsBacktestSessionCheckResponse]
    counts: OptionsBacktestDataCountsResponse
    first_testable_session_open_ms: Ms | None = None
    last_testable_session_open_ms: Ms | None = None
    longest_testable_stretch: OptionsBacktestTestableStretchResponse | None = None

    @classmethod
    def from_check(cls, check: DataCheck) -> Self:
        first, last, stretch = check.first_testable, check.last_testable, check.longest_testable_stretch
        return cls(
            option_prices_from_ms=et_midnight_ms(check.option_prices_from),
            sessions=[OptionsBacktestSessionCheckResponse.from_session(session) for session in check.sessions],
            counts=OptionsBacktestDataCountsResponse(
                ready=check.count(SessionVerdict.READY),
                needs_download=check.count(SessionVerdict.NEEDS_DOWNLOAD),
                cannot_be_tested=check.count(SessionVerdict.CANNOT_BE_TESTED),
            ),
            first_testable_session_open_ms=None if first is None else first.session_open_ms,
            last_testable_session_open_ms=None if last is None else last.session_open_ms,
            longest_testable_stretch=None
            if stretch is None
            else OptionsBacktestTestableStretchResponse.from_stretch(stretch),
        )


# ── A run ────────────────────────────────────────────────────────────────


class OptionsBacktestRateResponse(BaseModel):
    """A rate of two counts: the numerator and the denominator, and the quotient rounded half-even to six places."""

    numerator: str
    denominator: str
    rounded_to_6_places: str

    @classmethod
    def from_ratio(cls, ratio: Ratio) -> Self:
        return cls(
            numerator=exact(ratio.numerator),
            denominator=exact(ratio.denominator),
            rounded_to_6_places=exact(ratio.rounded_to_6_places()),
        )


class OptionsBacktestDollarRateResponse(BaseModel):
    """A rate of two sums of dollars: the numerator and the denominator, and the quotient rounded to six places."""

    numerator_usd: str
    denominator_usd: str
    rounded_to_6_places: str

    @classmethod
    def from_ratio(cls, ratio: Ratio) -> Self:
        return cls(
            numerator_usd=exact(ratio.numerator),
            denominator_usd=exact(ratio.denominator),
            rounded_to_6_places=exact(ratio.rounded_to_6_places()),
        )


class OptionsBacktestDrawdownResponse(BaseModel):
    """The largest fall of equity from its running peak, measured at the end of each session."""

    peak_equity_usd: str
    trough_equity_usd: str
    fall_usd: str
    fall_ratio: OptionsBacktestDollarRateResponse
    # ``None`` when the peak is the starting cash, before the first session.
    peak_session_open_ms: Ms | None = None
    trough_session_open_ms: Ms
    measured_at: Literal["session_end"]

    @classmethod
    def from_drawdown(cls, drawdown: Drawdown) -> Self:
        return cls(
            peak_equity_usd=exact(drawdown.peak_equity_usd),
            trough_equity_usd=exact(drawdown.trough_equity_usd),
            fall_usd=exact(drawdown.fall_usd),
            fall_ratio=OptionsBacktestDollarRateResponse.from_ratio(drawdown.fall_ratio),
            peak_session_open_ms=drawdown.peak_session_open_ms,
            trough_session_open_ms=drawdown.trough_session_open_ms,
            measured_at=drawdown.measured_at,
        )


class OptionsBacktestSessionStatusCountResponse(BaseModel):
    status: SessionStatusWord
    count: int


class OptionsBacktestSkipCountResponse(BaseModel):
    reason: SkipReasonWord
    count: int


class OptionsBacktestExitCountResponse(BaseModel):
    reason: ExitReasonWord
    count: int


class OptionsBacktestPickRefusalCountResponse(BaseModel):
    reason: PickRefusalWord
    count: int


class OptionsBacktestDayRowResponse(BaseModel):
    """One session of the by-day table. Won, lost and break-even count trades, not sessions."""

    session_open_ms: Ms
    status: SessionStatusWord
    skip_reason: SkipReasonWord | None = None
    trades: int
    net_profit_usd: str
    fees_usd: str
    trades_won: int
    trades_lost: int
    trades_break_even: int

    @classmethod
    def from_row(cls, row: DayRow) -> Self:
        return cls(
            session_open_ms=row.session_open_ms,
            status=row.status.value,  # type: ignore[arg-type]
            skip_reason=None if row.skip_reason is None else row.skip_reason.value,  # type: ignore[arg-type]
            trades=row.trades,
            net_profit_usd=exact(row.net_profit_usd),
            fees_usd=exact(row.fees_usd),
            trades_won=row.trades_won,
            trades_lost=row.trades_lost,
            trades_break_even=row.trades_break_even,
        )


class OptionsBacktestMonthRowResponse(BaseModel):
    """The sessions of one New York calendar month the run covered."""

    year: int
    month: int
    sessions: int
    sessions_traded: int
    trades: int
    net_profit_usd: str
    fees_usd: str
    trades_won: int
    trades_lost: int
    trades_break_even: int
    sessions_by_status: list[OptionsBacktestSessionStatusCountResponse]

    @classmethod
    def from_row(cls, row: MonthRow) -> Self:
        return cls(
            year=row.year,
            month=row.month,
            sessions=row.sessions,
            sessions_traded=row.sessions_traded,
            trades=row.trades,
            net_profit_usd=exact(row.net_profit_usd),
            fees_usd=exact(row.fees_usd),
            trades_won=row.trades_won,
            trades_lost=row.trades_lost,
            trades_break_even=row.trades_break_even,
            sessions_by_status=[
                OptionsBacktestSessionStatusCountResponse(status=status.value, count=count)  # type: ignore[arg-type]
                for status, count in row.sessions_by_status
            ],
        )


class OptionsBacktestCapitalInvalidEventResponse(BaseModel):
    """The first event that ended capital evidence, named by its session and, for a close, its minute."""

    kind: CapitalInvalidKindWord
    session_open_ms: Ms
    # The minute of an unfunded closing group; ``None`` for a session that ended with a position marked.
    at_ms: Ms | None = None
    # Free cash after an unfunded closing group, below zero; ``None`` for a session that ended marked.
    free_cash_after_usd: str | None = None

    @classmethod
    def from_event(cls, event: CapitalInvalidEvent) -> Self:
        return cls(
            kind=event.kind.value,  # type: ignore[arg-type]
            session_open_ms=event.session_open_ms,
            at_ms=event.at_ms,
            free_cash_after_usd=_exact_or_none(event.free_cash_after_usd),
        )


class OptionsBacktestCapitalEvidenceResponse(BaseModel):
    """How far the cash provably carried a run, and each figure of its money on its own.

    None of the five figures stands in for another, and none is the drawdown, which is marked equity's at the ends of
    sessions. The totals keep every session whatever this says.
    """

    sessions_tested: int
    # The sessions that ended before the event that ended capital evidence, or all of them when there was none.
    sessions_funded_continuously: int
    # ``None`` when the cash carried the whole run.
    ended_by: OptionsBacktestCapitalInvalidEventResponse | None = None
    most_collateral_held_usd: str
    # The cash this path needed over the continuously funded stretch: true of this recorded path only, and never a
    # minimum or a safe amount.
    cash_needed_usd: str
    # An entry in that stretch was refused for cash, so a larger starting cash would have traded a different path.
    cash_needed_is_incomplete: bool
    # The least free cash seen, below zero included.
    least_free_cash_usd: str
    # The largest assignment obligation held: sold contracts times strike times multiplier, gross, and not a loss.
    largest_assignment_obligation_usd: str
    # Closes that bought back a sold leg with its cover held and for more than the free cash before them.
    closes_relied_on_own_relief: int

    @classmethod
    def from_evidence(cls, evidence: CapitalEvidence) -> Self:
        return cls(
            sessions_tested=evidence.sessions_tested,
            sessions_funded_continuously=evidence.sessions_funded_continuously,
            ended_by=None
            if evidence.ended_by is None
            else OptionsBacktestCapitalInvalidEventResponse.from_event(evidence.ended_by),
            most_collateral_held_usd=exact(evidence.most_collateral_held_usd),
            cash_needed_usd=exact(evidence.cash_needed_usd),
            cash_needed_is_incomplete=evidence.cash_needed_is_incomplete,
            least_free_cash_usd=exact(evidence.least_free_cash_usd),
            largest_assignment_obligation_usd=exact(evidence.largest_assignment_obligation_usd),
            closes_relied_on_own_relief=evidence.closes_relied_on_own_relief,
        )


class OptionsBacktestCashLevelResponse(BaseModel):
    """The whole run replayed at one starting cash. No level is called safe."""

    # The level as a multiple of the setup's starting cash, and the cash it started with.
    multiple: str
    starting_cash_usd: str
    # The level whose cash is the setup's own: this level is the run's own result.
    is_setups_own: bool
    entries_filled: int
    entries_refused_for_cash: int
    net_profit_usd: str
    capital_evidence: OptionsBacktestCapitalEvidenceResponse

    @classmethod
    def from_level(cls, level: CashLevel) -> Self:
        return cls(
            multiple=exact(level.multiple),
            starting_cash_usd=exact(level.starting_cash_usd),
            is_setups_own=level.is_setups_own,
            entries_filled=level.entries_filled,
            entries_refused_for_cash=level.entries_refused_for_cash,
            net_profit_usd=exact(level.net_profit_usd),
            capital_evidence=OptionsBacktestCapitalEvidenceResponse.from_evidence(level.capital_evidence),
        )


class OptionsBacktestRunStampsResponse(BaseModel):
    """The rules a run was made under: today's, and no evidence of what a broker required on a session's date."""

    replay_rules_version: int
    # The requirement rule that held the collateral, by name.
    requirement_rule: str
    # New York midnight of the day the requirement rule's page was read.
    requirement_rule_read_at_ms: Ms

    @classmethod
    def from_stamps(cls, stamps: RunStamps) -> Self:
        return cls(
            replay_rules_version=stamps.replay_rules_version,
            requirement_rule=stamps.requirement_rule,
            requirement_rule_read_at_ms=stamps.requirement_rule_read_at_ms,
        )


class OptionsBacktestRunFiguresResponse(BaseModel):
    """Every figure of a complete run, each named for what it counts. A trade is one leg round trip."""

    sessions_tested: int
    sessions_traded: int
    sessions_no_trade: int
    sessions_skipped: int
    skipped_by_reason: list[OptionsBacktestSkipCountResponse]
    sessions_incomplete: int
    sessions_profitable: int
    sessions_losing: int
    sessions_break_even: int
    # Profitable sessions over traded sessions; ``None`` when no session traded.
    daily_win_rate: OptionsBacktestRateResponse | None = None
    trades: int
    trades_won: int
    trades_lost: int
    trades_break_even: int
    # Won trades over trades; ``None`` when there was no trade.
    trade_win_rate: OptionsBacktestRateResponse | None = None
    exits_by_reason: list[OptionsBacktestExitCountResponse]
    starting_cash_usd: str
    net_profit_usd: str
    gross_profit_usd: str
    # The losses of the lost trades added up, as a positive number.
    gross_loss_usd: str
    # Gross profit over gross loss; ``None`` when no trade lost.
    profit_factor: OptionsBacktestDollarRateResponse | None = None
    total_fees_usd: str
    # What the fills cost beyond the prices seen: slippage and the rounding to the price step together.
    slippage_paid_usd: str
    ending_cash_usd: str
    drawdown_by_fraction: OptionsBacktestDrawdownResponse | None = None
    drawdown_by_dollars: OptionsBacktestDrawdownResponse | None = None
    sessions_without_momentum: int
    entries_refused_for_cash: int
    re_entries_blocked_by_cutoff: int
    entries_dropped: int
    picks_refused_by_reason: list[OptionsBacktestPickRefusalCountResponse]
    legs_without_a_starting_price: int
    # A re-entry that ended without entering, whatever the cause; each is also counted under its own heading above, so
    # this overlaps them and is not to be added to them.
    re_entries_lost: int
    by_day: list[OptionsBacktestDayRowResponse]
    by_month: list[OptionsBacktestMonthRowResponse]
    # Whether the cash could have carried the run, apart from every figure above, which it never changes. ``None`` on
    # each reads "not recorded": the run was stored before they were.
    capital_evidence: OptionsBacktestCapitalEvidenceResponse | None = None
    # The run replayed at each starting-cash level, lowest cash first, the setup's own among them.
    cash_ladder: list[OptionsBacktestCashLevelResponse] | None = None
    stamps: OptionsBacktestRunStampsResponse | None = None

    @classmethod
    def from_summary(cls, summary: RunSummary) -> Self:
        def rate(ratio: Ratio | None) -> OptionsBacktestRateResponse | None:
            return None if ratio is None else OptionsBacktestRateResponse.from_ratio(ratio)

        def drawdown(each: Drawdown | None) -> OptionsBacktestDrawdownResponse | None:
            return None if each is None else OptionsBacktestDrawdownResponse.from_drawdown(each)

        return cls(
            sessions_tested=summary.sessions_tested,
            sessions_traded=summary.sessions_traded,
            sessions_no_trade=summary.sessions_no_trade,
            sessions_skipped=summary.sessions_skipped,
            skipped_by_reason=[
                OptionsBacktestSkipCountResponse(reason=reason.value, count=count)  # type: ignore[arg-type]
                for reason, count in summary.skipped_by_reason
            ],
            sessions_incomplete=summary.sessions_incomplete,
            sessions_profitable=summary.sessions_profitable,
            sessions_losing=summary.sessions_losing,
            sessions_break_even=summary.sessions_break_even,
            daily_win_rate=rate(summary.daily_win_rate),
            trades=summary.trades,
            trades_won=summary.trades_won,
            trades_lost=summary.trades_lost,
            trades_break_even=summary.trades_break_even,
            trade_win_rate=rate(summary.trade_win_rate),
            exits_by_reason=[
                OptionsBacktestExitCountResponse(reason=reason.value, count=count)  # type: ignore[arg-type]
                for reason, count in summary.exits_by_reason
            ],
            starting_cash_usd=exact(summary.starting_cash_usd),
            net_profit_usd=exact(summary.net_profit_usd),
            gross_profit_usd=exact(summary.gross_profit_usd),
            gross_loss_usd=exact(summary.gross_loss_usd),
            profit_factor=None
            if summary.profit_factor is None
            else OptionsBacktestDollarRateResponse.from_ratio(summary.profit_factor),
            total_fees_usd=exact(summary.total_fees_usd),
            slippage_paid_usd=exact(summary.slippage_paid_usd),
            ending_cash_usd=exact(summary.ending_cash_usd),
            drawdown_by_fraction=drawdown(summary.drawdown_by_fraction),
            drawdown_by_dollars=drawdown(summary.drawdown_by_dollars),
            sessions_without_momentum=summary.sessions_without_momentum,
            entries_refused_for_cash=summary.entries_refused_for_cash,
            re_entries_blocked_by_cutoff=summary.re_entries_blocked_by_cutoff,
            entries_dropped=summary.entries_dropped,
            picks_refused_by_reason=[
                OptionsBacktestPickRefusalCountResponse(reason=reason.value, count=count)  # type: ignore[arg-type]
                for reason, count in summary.picks_refused_by_reason
            ],
            legs_without_a_starting_price=summary.legs_without_a_starting_price,
            re_entries_lost=summary.re_entries_lost,
            by_day=[OptionsBacktestDayRowResponse.from_row(row) for row in summary.by_day],
            by_month=[OptionsBacktestMonthRowResponse.from_row(row) for row in summary.by_month],
            capital_evidence=None
            if summary.capital_evidence is None
            else OptionsBacktestCapitalEvidenceResponse.from_evidence(summary.capital_evidence),
            cash_ladder=None
            if summary.cash_ladder is None
            else [OptionsBacktestCashLevelResponse.from_level(level) for level in summary.cash_ladder],
            stamps=None if summary.stamps is None else OptionsBacktestRunStampsResponse.from_stamps(summary.stamps),
        )


class OptionsBacktestSlippagePointResponse(BaseModel):
    """The whole run replayed at one slippage."""

    slippage_percent: str
    # The slippage the setup itself carries: this point is the run's own result.
    is_setups_own: bool
    net_profit_usd: str
    trades: int
    traded_sessions: int
    winning_sessions: int


class OptionsBacktestSlippageToleranceResponse(BaseModel):
    """How much slippage the result survives, read off the ladder it was replayed at."""

    # The highest ladder value at which, and at every lower value, net profit is above zero; ``None`` when even the
    # lowest is not.
    highest_profitable_percent: str | None = None
    # The next ladder value up, where net profit is not above zero; ``None`` when every value was profitable, which
    # reads "survives the whole ladder" and not "survives any slippage".
    first_unprofitable_percent: str | None = None
    # No trade was made at any ladder value, so there was no result to survive; both percents are then ``None``.
    nothing_traded: bool


class OptionsBacktestFillsBannerResponse(BaseModel):
    """The facts the result's banner states: how fills were made and what they cost."""

    fills_are_simulated_from_trade_prices: bool
    bid_and_ask_are_available: bool
    slippage_percent: str
    price_step_usd: str
    fee_policy_kind: Literal["estimated", "per_contract", "not_charged"]
    fee_per_contract_usd: str | None = None
    # The fee policy in a sentence, written when the run was made.
    fee_policy: str


class OptionsBacktestRunFailureItemResponse(BaseModel):
    """One thing that failed: a session (by its open) or the run as a whole (``None``)."""

    session_open_ms: Ms | None = None
    code: str
    message: str

    @classmethod
    def from_item(cls, item: FailureItem) -> Self:
        return cls(session_open_ms=item.session_open_ms, code=item.code, message=item.message)


class OptionsBacktestRunFailureResponse(BaseModel):
    """Why a run failed: a code to branch on, a plain reason, and the list of what failed."""

    code: str
    reason: str
    items: list[OptionsBacktestRunFailureItemResponse]

    @classmethod
    def from_failure(cls, failure: RunFailure) -> Self:
        return cls(
            code=failure.code,
            reason=failure.reason,
            items=[OptionsBacktestRunFailureItemResponse.from_item(item) for item in failure.items],
        )


class OptionsBacktestRunHeadlineResponse(BaseModel):
    sessions_tested: int
    sessions_traded: int
    trades: int
    trades_won: int
    net_profit_usd: str
    ending_cash_usd: str

    @classmethod
    def from_headline(cls, headline: Headline) -> Self:
        return cls(
            sessions_tested=headline.sessions_tested,
            sessions_traded=headline.sessions_traded,
            trades=headline.trades,
            trades_won=headline.trades_won,
            net_profit_usd=exact(headline.net_profit_usd),
            ending_cash_usd=exact(headline.ending_cash_usd),
        )


class OptionsBacktestRunListItemResponse(BaseModel):
    """A run in the history. ``interrupted`` is how a running run reads when its job is gone."""

    id: Annotated[int, Field(ge=1, le=RUN_ID_MAX)]
    job_id: str
    created_at_ms: Ms
    finished_at_ms: Ms | None = None
    status: RunStatusWord
    # The name the setup was saved under, when the run was started from a saved setup.
    setup_name: str | None = None
    underlying: str
    first_session_open_ms: Ms
    last_session_open_ms: Ms
    sessions_requested: int
    # The fee policy in a sentence, written when the run was made.
    fee_policy: str
    headline: OptionsBacktestRunHeadlineResponse | None = None
    failure: OptionsBacktestRunFailureResponse | None = None

    @classmethod
    def from_record(cls, record: RunRecord) -> Self:
        return cls(
            id=record.id,
            job_id=record.job_id,
            created_at_ms=record.created_at_ms,
            finished_at_ms=record.finished_at_ms,
            status=record.status,
            setup_name=record.setup_name,
            underlying=record.underlying,
            first_session_open_ms=record.first_session_open_ms,
            last_session_open_ms=record.last_session_open_ms,
            sessions_requested=record.sessions_requested,
            fee_policy=record.fee_policy_text,
            headline=None
            if record.headline is None
            else OptionsBacktestRunHeadlineResponse.from_headline(record.headline),
            failure=None if record.failure is None else OptionsBacktestRunFailureResponse.from_failure(record.failure),
        )


class OptionsBacktestRunDetailResponse(OptionsBacktestRunListItemResponse):
    """One run, opened: the setup it ran, the banner facts and, when it is complete, every figure. Not its trades."""

    calendar_identity: str
    calendar_package_version: str
    # The setup the run was made with, read again with today's rules. A run whose stored setup no longer validates still
    # opens, as a saved setup does: ``setup`` is ``None`` and ``setup_cannot_open_reason`` says why. Exactly one of the
    # two is set. The ``banner`` states what the setup says of fills and fees, so it is set exactly when the setup is;
    # the fee policy in words is on the run itself (``fee_policy``) either way, and so are the figures.
    setup: OptionsBacktestSetup | None = None
    setup_cannot_open_reason: str | None = None
    banner: OptionsBacktestFillsBannerResponse | None = None
    figures: OptionsBacktestRunFiguresResponse | None = None
    # The whole run replayed at each slippage on the ladder, lowest first; empty unless the run is complete.
    slippage: list[OptionsBacktestSlippagePointResponse]
    slippage_tolerance: OptionsBacktestSlippageToleranceResponse | None = None

    @model_validator(mode="after")
    def _either_the_setup_or_why_not(self) -> Self:
        if (self.setup is None) == (self.setup_cannot_open_reason is None):
            raise ValueError("a run carries its setup, or the reason it cannot be opened, and not both")
        if (self.banner is None) != (self.setup is None):
            raise ValueError("a run carries its banner exactly when it carries its setup")
        return self

    @classmethod
    def from_stored(cls, stored: StoredRun) -> Self:
        setup, figures = stored.setup, stored.figures
        listed = OptionsBacktestRunListItemResponse.from_record(stored.record)
        tolerance = None if figures is None else figures.slippage_tolerance
        return cls(
            **listed.model_dump(),
            calendar_identity=stored.record.calendar_identity,
            calendar_package_version=stored.record.calendar_package_version,
            setup=None if setup is None else served_setup(setup),
            setup_cannot_open_reason=stored.setup_cannot_open_reason,
            banner=None
            if setup is None
            else OptionsBacktestFillsBannerResponse(
                fills_are_simulated_from_trade_prices=True,
                bid_and_ask_are_available=False,
                slippage_percent=exact(setup.slippage_percent),
                price_step_usd=exact(setup.price_step_usd),
                fee_policy_kind=setup.fees.kind,
                fee_per_contract_usd=_exact_or_none(setup.fees.per_contract_usd),
                fee_policy=stored.record.fee_policy_text,
            ),
            figures=None if figures is None else OptionsBacktestRunFiguresResponse.from_summary(figures.summary),
            slippage=[]
            if figures is None
            else [
                OptionsBacktestSlippagePointResponse(
                    slippage_percent=exact(point.slippage_percent),
                    is_setups_own=point.is_setups_own,
                    net_profit_usd=exact(point.net_profit_usd),
                    trades=point.trades,
                    traded_sessions=point.traded_sessions,
                    winning_sessions=point.winning_sessions,
                )
                for point in figures.slippage
            ],
            slippage_tolerance=None
            if tolerance is None
            else OptionsBacktestSlippageToleranceResponse(
                highest_profitable_percent=_exact_or_none(tolerance.highest_profitable_percent),
                first_unprofitable_percent=_exact_or_none(tolerance.first_unprofitable_percent),
                nothing_traded=tolerance.nothing_traded,
            ),
        )


# ── The job ──────────────────────────────────────────────────────────────


class OptionsBacktestJobRequest(BaseModel):
    """Body of ``POST /api/jobs-internal/options-backtest``: the setup, the half-open range, and the minted job id.

    The range is ``[start_ms, end_ms)`` as Grid Search's window is: New York midnight of the first date to New York
    midnight after the last. ``setup_name`` is the name the setup was saved under, when it was started from one.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=JOB_ID_MAX_LENGTH, pattern=JOB_ID_PATTERN)
    setup: OptionsBacktestSetup
    start_ms: Ms = Field(description=START_MS_RULE)
    end_ms: Ms = Field(description=END_MS_RULE)
    setup_name: Annotated[str, Field(pattern=SETUP_NAME_PATTERN)] | None = Field(
        default=None, description=RUN_SETUP_NAME_RULE
    )


class OptionsBacktestJobAcceptedResponse(BaseModel):
    """The run exists, ``running``, and a worker holds it."""

    job_id: str
    run_id: Annotated[int, Field(ge=1, le=RUN_ID_MAX)]
    status: Literal["queued"]


class OptionsBacktestJobResult(BaseModel):
    """What a completed job leaves at ``/api/jobs/{id}/result``; the run itself is read from the history."""

    run_id: int
    status: Literal["complete"]
    sessions_tested: int
    trades: int
    net_profit_usd: str
