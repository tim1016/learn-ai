"""Check data: which sessions of a range a run could test, from what is already stored (#3030).

Formula: none. For each session of a range the answer is one of
    ready              everything the session can need is stored, or the session is skipped and needs nothing
    needs_download     it can be tested once data is fetched, and what is missing is named
    cannot_be_tested   with every plain reason
Reference: the run's own order of work (``run_sessions``): a session whose window does not fit reads nothing; any
  other reads the underlying's day and the listing, then the contracts ``session_needs`` names. The check asks the
  same questions in the same order, so it and the run cannot disagree about what a session needs.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_data_check.py.

Nothing is fetched and no vendor is asked. The lake decides, and this file restates none of its rules: whether a
session is too old for the plan (``option_lake.plan_window_refusal``), whether its data is final yet
(``option_lake.session_refusal``), and whether the fee model prices a date (``fees.estimated_fees_available``). What
is stored is read the way the run reads it, through ``RunData``, so "stored" here means "the run could read it".
The contracts a session needs are known only once its underlying day and its listing are stored; until then the
answer says so, and gives no number.

A recorded failure is something the lake wrote down when it last tried to fetch a thing. The lake asks again on a
download until a row has spent its attempts, so a recorded failure makes a session ``cannot_be_tested`` only when the
lake will not ask again; otherwise the session needs a download, and the note says the last try failed and why.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_lake import option_history_floor, plan_window_refusal, session_refusal
from app.data_lake.option_listing import OptionListingFileError
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.data import RunData
from app.options_backtest.fees import estimated_fees_available
from app.options_backtest.lake_data import UnderlyingDayUnreadable
from app.options_backtest.needs import session_needs
from app.options_backtest.results import SkipReason
from app.options_backtest.session_market import MalformedBars, bars_by_close
from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.session_anchors import et_date_at_ms


class SessionVerdict(StrEnum):
    READY = "ready"
    NEEDS_DOWNLOAD = "needs_download"
    CANNOT_BE_TESTED = "cannot_be_tested"


class ProblemCode(StrEnum):
    """Why a session cannot be tested. A session can have several."""

    # The session is older than the options plan reaches (the lake says so).
    OLDER_THAN_THE_DATA_PLAN = "older_than_the_data_plan"
    # The session's data is not final yet, so the lake will not take it (the lake says so).
    SESSION_DATA_NOT_FINAL = "session_data_not_final"
    NO_PUBLISHED_FEE_RATES = "no_published_fee_rates"
    # The lake wrote down a failure for something the session needs and will not try it again.
    LAKE_HOLDS_A_FAILURE = "lake_holds_a_failure"
    # Stored bars that break the rules of a bar (a repeat, a price of zero): fetching them again cannot mend that.
    STORED_DATA_IS_MALFORMED = "stored_data_is_malformed"


class DataKind(StrEnum):
    """A thing a download can bring."""

    UNDERLYING_BARS = "underlying_bars"
    LISTING = "listing"
    CONTRACTS = "contracts"


@dataclass(frozen=True)
class DataThing:
    """One stored thing of one session: the underlying's day, the listing, or one contract's day."""

    kind: DataKind
    contract: OptionContract | None = None


@dataclass(frozen=True)
class RecordedFailure:
    """What the lake wrote down the last time it failed to fetch a thing."""

    reason: str
    # False when the row has spent its attempts and the lake refuses to ask again.
    will_be_asked_again: bool


class CheckableData(RunData, Protocol):
    """What the check reads: everything a run reads, and the lake's record of failures. Reads only; no vendor call."""

    def recorded_failure(self, thing: DataThing, trading_date: date) -> RecordedFailure | None: ...


@dataclass(frozen=True)
class Problem:
    code: ProblemCode
    message: str
    # The lake's own reason code, when the problem is a failure it recorded.
    lake_reason: str | None = None


@dataclass(frozen=True)
class ContractNeeds:
    """How many contract days a session needs and how many are not stored; ``None`` while that is not yet known."""

    needed: int | None
    missing: int | None

    @property
    def known(self) -> bool:
        return self.needed is not None


NOT_YET_KNOWN = ContractNeeds(needed=None, missing=None)


@dataclass(frozen=True)
class SessionCheck:
    """One session of a range, checked. A session is identified by its open in ms."""

    session_open_ms: int
    session_close_ms: int
    verdict: SessionVerdict
    # Set on a ready session that the replay will skip, and says which rule skips it.
    skip: SkipReason | None
    # What a download must bring; empty unless the verdict is ``needs_download``.
    missing: tuple[DataKind, ...]
    contracts: ContractNeeds
    # Set when, and only when, the verdict is ``cannot_be_tested``.
    problems: tuple[Problem, ...]
    # Plain words about anything else worth knowing: a damaged stored file, an earlier try that failed.
    notes: tuple[str, ...]

    @property
    def trading_date(self) -> date:
        """The New York date of the session's open. Derived, never stored or sent."""
        return et_date_at_ms(self.session_open_ms)

    @property
    def testable(self) -> bool:
        return self.verdict is not SessionVerdict.CANNOT_BE_TESTED


@dataclass(frozen=True)
class TestableStretch:
    """A run of consecutive sessions of the range that can all be tested, to offer as a new range."""

    __test__ = False  # not a pytest class

    first_session_open_ms: int
    last_session_open_ms: int
    sessions: int

    @property
    def first(self) -> date:
        return et_date_at_ms(self.first_session_open_ms)

    @property
    def last(self) -> date:
        return et_date_at_ms(self.last_session_open_ms)


@dataclass(frozen=True)
class DataCheck:
    sessions: tuple[SessionCheck, ...]
    # The first date the options plan serves minute bars for, as of the check.
    option_prices_from: date

    def count(self, verdict: SessionVerdict) -> int:
        return sum(1 for session in self.sessions if session.verdict is verdict)

    @property
    def first_testable(self) -> SessionCheck | None:
        return next((session for session in self.sessions if session.testable), None)

    @property
    def last_testable(self) -> SessionCheck | None:
        return next((session for session in reversed(self.sessions) if session.testable), None)

    @property
    def longest_testable_stretch(self) -> TestableStretch | None:
        """The longest unbroken run of testable sessions; of two as long, the earlier."""
        best: tuple[int, int] | None = None  # (start index, length)
        start = None
        for at, session in enumerate((*self.sessions, None)):
            if session is not None and session.testable:
                start = at if start is None else start
                continue
            if start is not None and (best is None or at - start > best[1]):
                best = (start, at - start)
            start = None
        if best is None:
            return None
        first, last = self.sessions[best[0]], self.sessions[best[0] + best[1] - 1]
        return TestableStretch(
            first_session_open_ms=first.session_open_ms,
            last_session_open_ms=last.session_open_ms,
            sessions=best[1],
        )


# A stored file that is there and is not usable. The lake's capture reads the same files and fetches them again.
_UNUSABLE_FILE = (LakeAdmissionError, OptionListingFileError, OptionBarsFileError, UnderlyingDayUnreadable)

SESSION_DATA_NOT_FINAL_WORDS = "The session's data is not final yet; it can be fetched from the next New York day."
_OLDER_THAN_THE_PLAN = "This session is older than the data plan reaches, so option prices for it cannot be fetched."


def check_data(setup: OptionsBacktestSetup, sessions: Sequence[date], *, now_ms: int, data: CheckableData) -> DataCheck:
    """Check each of ``sessions`` (NYSE sessions, in date order) for ``setup``, as of the instant ``now_ms``.

    Reads what is stored through ``data`` and asks the lake nothing that costs a claim or a vendor request. Blocking:
    call it from a worker thread.
    """
    return DataCheck(
        sessions=tuple(_check_session(setup, day, now_ms=now_ms, data=data) for day in sessions),
        option_prices_from=option_history_floor(et_date_at_ms(now_ms)),
    )


def _check_session(setup: OptionsBacktestSetup, day: date, *, now_ms: int, data: CheckableData) -> SessionCheck:
    window = session_window_for_date(day)
    problems: list[Problem] = []
    # The run refuses an estimated fee for any session of the range before it reads anything, a skipped one too.
    if setup.fees.kind == "estimated" and not estimated_fees_available(day):
        problems.append(
            Problem(
                ProblemCode.NO_PUBLISHED_FEE_RATES,
                "Estimated fees cannot be priced on this date: the published option fee rates do not reach it. "
                "Choose a fee per contract, or no fee, or start later.",
            )
        )

    def result(verdict: SessionVerdict, **fields: object) -> SessionCheck:
        defaults: dict[str, object] = {
            "skip": None,
            "missing": (),
            "contracts": NOT_YET_KNOWN,
            "problems": tuple(problems),
            "notes": (),
        }
        return SessionCheck(window.open_ms_utc, window.close_ms_utc, verdict, **{**defaults, **fields})  # type: ignore[arg-type]

    if not setup.window_fits(session_open_ms=window.open_ms_utc, session_close_ms=window.close_ms_utc):
        # A skipped session reads nothing, so what the lake would say of its data does not matter.
        if problems:
            return result(SessionVerdict.CANNOT_BE_TESTED)
        return result(SessionVerdict.READY, skip=SkipReason.WINDOW_DOES_NOT_FIT, contracts=ContractNeeds(0, 0))

    problems.extend(_lake_refusals(setup.underlying, day, now_ms=now_ms))
    if problems:
        return result(SessionVerdict.CANNOT_BE_TESTED)
    return _inspect_what_is_stored(setup, day, window.open_ms_utc, window.close_ms_utc, data, result)


def _lake_refusals(underlying: str, day: date, *, now_ms: int) -> list[Problem]:
    refusals: list[Problem] = []
    if session_refusal(underlying, day, now_ms=now_ms) is not None:
        refusals.append(Problem(ProblemCode.SESSION_DATA_NOT_FINAL, SESSION_DATA_NOT_FINAL_WORDS))
    if plan_window_refusal(underlying, day, now_ms=now_ms) is not None:
        refusals.append(Problem(ProblemCode.OLDER_THAN_THE_DATA_PLAN, _OLDER_THAN_THE_PLAN))
    return refusals


def _stored[T](read: Callable[[], T | None], what: str, notes: list[str]) -> T | None:
    """A read that answers ``None`` for a file that is not there and for one that is there and unusable."""
    try:
        return read()
    except _UNUSABLE_FILE as exc:
        notes.append(f"The stored {what} cannot be read ({type(exc).__name__}) and will be fetched again.")
        return None


def _inspect_what_is_stored(
    setup: OptionsBacktestSetup,
    day: date,
    open_ms: int,
    close_ms: int,
    data: CheckableData,
    result: Callable[..., SessionCheck],
) -> SessionCheck:
    notes: list[str] = []
    underlying = _stored(lambda: data.underlying_day(day), "underlying bars", notes)
    listing = _stored(lambda: data.listing(day), "contract listing", notes)
    absent = [
        (DataKind.UNDERLYING_BARS, underlying is None),
        (DataKind.LISTING, listing is None),
    ]
    missing = [kind for kind, is_absent in absent if is_absent]
    if underlying is None or listing is None:
        return _what_a_download_must_bring(day, missing, data, notes, result, contracts=NOT_YET_KNOWN)

    try:
        bars_by_close(underlying.value, trading_date=day, what="the underlying")
    except MalformedBars as exc:
        return _malformed(result, f"The stored underlying bars are malformed: {exc}")
    needs = session_needs(
        setup,
        trading_date=day,
        session_open_ms=open_ms,
        session_close_ms=close_ms,
        underlying=tuple(underlying.value),
        listing=listing.value,
    )
    if needs.skip_reason is not None:
        return result(SessionVerdict.READY, skip=needs.skip_reason, contracts=ContractNeeds(0, 0), notes=tuple(notes))

    absent_contracts: list[OptionContract] = []
    for contract in sorted(needs.contracts, key=lambda each: each.occ_symbol):
        stored = _stored(lambda c=contract: data.option_day(c, day), f"bars of {contract.occ_symbol}", notes)
        if stored is None:
            absent_contracts.append(contract)
            continue
        try:
            bars_by_close(stored.value, trading_date=day, what=contract.occ_symbol)
        except MalformedBars as exc:
            return _malformed(result, f"The stored bars of {contract.occ_symbol} are malformed: {exc}")
    contracts = ContractNeeds(needed=len(needs.contracts), missing=len(absent_contracts))
    if not absent_contracts:
        return result(SessionVerdict.READY, contracts=contracts, notes=tuple(notes))
    return _what_a_download_must_bring(
        day, [DataKind.CONTRACTS], data, notes, result, contracts=contracts, absent=absent_contracts
    )


def _malformed(result: Callable[..., SessionCheck], message: str) -> SessionCheck:
    return result(
        SessionVerdict.CANNOT_BE_TESTED,
        problems=(Problem(ProblemCode.STORED_DATA_IS_MALFORMED, message),),
    )


def _what_a_download_must_bring(
    day: date,
    missing: list[DataKind],
    data: CheckableData,
    notes: list[str],
    result: Callable[..., SessionCheck],
    *,
    contracts: ContractNeeds,
    absent: Sequence[OptionContract] = (),
) -> SessionCheck:
    """``needs_download`` for the missing things, unless the lake has written one down as a failure it will not retry."""
    things = [
        DataThing(kind) if kind is not DataKind.CONTRACTS else DataThing(kind, contract)
        for kind in missing
        for contract in (absent if kind is DataKind.CONTRACTS else (None,))
    ]
    refused: list[Problem] = []
    for thing in things:
        failure = data.recorded_failure(thing, day)
        if failure is None:
            continue
        label = _label(thing)
        if failure.will_be_asked_again:
            notes.append(f"The last try to fetch {label} failed ({failure.reason}); it will be asked for again.")
        else:
            refused.append(
                Problem(
                    ProblemCode.LAKE_HOLDS_A_FAILURE,
                    f"The lake recorded a failure fetching {label} ({failure.reason}) and will not try again.",
                    lake_reason=failure.reason,
                )
            )
    if refused:
        return result(SessionVerdict.CANNOT_BE_TESTED, problems=tuple(refused), contracts=contracts, notes=tuple(notes))
    return result(SessionVerdict.NEEDS_DOWNLOAD, missing=tuple(missing), contracts=contracts, notes=tuple(notes))


def _label(thing: DataThing) -> str:
    if thing.kind is DataKind.UNDERLYING_BARS:
        return "the underlying's bars"
    if thing.kind is DataKind.LISTING:
        return "the contract listing"
    assert thing.contract is not None  # a contracts thing always names its contract
    return f"the bars of {thing.contract.occ_symbol}"
