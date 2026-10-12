"""Check data: what each session of a range needs, from what is stored (#3030).

The checks run on dictionaries (``FakeRunData``), so nothing here touches a lake. The sessions are the consecutive
Wednesday to Friday of 2026-10-07 to 09, all full days; the half day is 2026-11-27, which closes at 13:00. "Now" is
2026-10-12 14:00 New York, a Monday: more than a day after the last of those sessions, so they are over under any
rule for when a session's data is final. The one test of a session that is not over puts "now" inside the session.

Hand-worked figures:

* The floor. The options plan reaches two years back with two days of headroom: 2026-10-12 less two years is
  2024-10-12, plus two days is 2024-10-14. A Monday: the floor day itself is taken, and the Friday before it
  (2024-10-11) is older than the plan.
* The setup of most tests buys one same-day at-the-money call at the entry time. The underlying closes at 500.20 in
  every minute, so the money is the 500 strike, and the one contract a session needs is the 500 call expiring that
  day. With a put leg added a session needs two.
* The half day. The window "enter 205 minutes after the open, leave 10 minutes before the close" fits a full day (a
  09:30 open is entered at 12:55 and the 16:00 close left at 15:50) and not the half day: a 13:00 close is left at
  12:50, before the 12:55 entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from itertools import pairwise
from typing import Any, get_args

import pytest

from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_lake import OptionFailureReason
from app.data_lake.types import ArtifactFailureReason
from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest import fetching
from app.options_backtest.data import Captured
from app.options_backtest.data_check import (
    LAKE_REASON_WORDS,
    CheckStopped,
    ContractNeeds,
    DataCheck,
    DataKind,
    DataThing,
    ProblemCode,
    RecordedFailure,
    SessionCheck,
    SessionVerdict,
    check_data,
    lake_reason_words,
)
from app.options_backtest.results import SkipReason
from app.options_backtest.setup import MS_PER_MINUTE
from tests.options_backtest._run_data import (
    FRI,
    HALF_DAY,
    THU,
    WED,
    Day,
    FakeRunData,
    contract_on,
    fake_data,
    flat,
    steady,
)
from tests.options_backtest._words import assert_plain
from tests.options_backtest.setup_fixtures import leg, setup

# 2026-10-12 14:00 New York.
NOW_MS = 1791828000000
CALL_500 = ("call", 500)
PUT_500 = ("put", 500)
NEEDED = {CALL_500: flat("1.00", 1, 390)}


class CheckData(FakeRunData):
    """A fake lake that also remembers what it failed to fetch, can hold a file that does not read, and says whether a
    download would replace the underlying's stored day (the lake's gate decides that; ``tests/options_backtest/
    test_unreadable_days.py`` holds the real answer for each kind of day)."""

    def __init__(
        self,
        base: FakeRunData,
        *,
        failures: Mapping[tuple[DataKind, date, str | None], RecordedFailure] | None = None,
        damaged: tuple[tuple[date, str], ...] = (),
        download_replaces_the_underlying: bool = False,
    ) -> None:
        super().__init__(underlying=base._underlying, listings=base._listings, options=base._options)
        self._failures = dict(failures or {})
        self._damaged = set(damaged)
        self._download_replaces_the_underlying = download_replaces_the_underlying

    def download_replaces_the_underlying(self, trading_date: date) -> bool:
        return self._download_replaces_the_underlying

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Any] | None:
        if (trading_date, contract.occ_symbol) in self._damaged:
            raise OptionBarsFileError("the stored bars do not read")
        return super().option_day(contract, trading_date)

    def recorded_failure(self, thing: DataThing, trading_date: date) -> RecordedFailure | None:
        symbol = None if thing.contract is None else thing.contract.occ_symbol
        return self._failures.get((thing.kind, trading_date, symbol))


def _open(day: date) -> int:
    """A session is identified by its open in ms."""
    return session_window_for_date(day).open_ms_utc


def _stored(**changes: Any) -> Day:
    return Day(underlying=steady("500.20"), options=dict(NEEDED), **changes)


def _check(
    days: dict[date, Day], *, sessions: list[date] | None = None, now_ms: int = NOW_MS, **kwargs: Any
) -> DataCheck:
    failures = kwargs.pop("failures", None)
    damaged = kwargs.pop("damaged", ())
    data = CheckData(fake_data(days, **kwargs), failures=failures, damaged=damaged)
    chosen = {"expiry": {"kind": "same_day"}}
    return check_data(setup(legs=[leg(**chosen)]), sessions or sorted(days), now_ms=now_ms, data=data)


def _only(check: DataCheck) -> SessionCheck:
    [session] = check.sessions
    return session


def test_a_session_with_everything_it_can_need_stored_is_ready_and_has_its_contract_counted() -> None:
    session = _only(_check({WED: _stored()}))

    assert session.verdict is SessionVerdict.READY
    assert session.skip is None and session.missing == () and session.problems == ()
    assert session.contracts == ContractNeeds(needed=1, missing=0)


def test_the_session_keeps_the_calendars_open_and_close() -> None:
    session = _only(_check({WED: _stored()}))

    window = session_window_for_date(WED)
    assert (session.session_open_ms, session.session_close_ms) == (window.open_ms_utc, window.close_ms_utc)


def test_missing_underlying_bars_are_named_and_the_contracts_are_not_yet_known() -> None:
    session = _only(_check({WED: _stored()}, without_underlying=(WED,)))

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.missing == (DataKind.UNDERLYING_BARS,)
    assert session.contracts == ContractNeeds(needed=None, missing=None) and not session.contracts.known


def test_a_missing_listing_is_named_and_the_contracts_are_not_yet_known() -> None:
    session = _only(_check({WED: _stored()}, without_listing=(WED,)))

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.missing == (DataKind.LISTING,)
    assert not session.contracts.known


def test_both_missing_are_named_in_the_order_a_download_brings_them() -> None:
    session = _only(_check({WED: _stored()}, without_underlying=(WED,), without_listing=(WED,)))

    assert session.missing == (DataKind.UNDERLYING_BARS, DataKind.LISTING)
    assert not session.contracts.known


def test_contracts_not_stored_are_counted_of_the_contracts_the_session_needs() -> None:
    # A call and a put at the money: two contracts. Only the call is stored.
    two_legs = setup(
        legs=[leg(expiry={"kind": "same_day"}), leg(leg_id="put", right="put", expiry={"kind": "same_day"})]
    )
    data = CheckData(fake_data({WED: _stored()}))

    session = _only(check_data(two_legs, [WED], now_ms=NOW_MS, data=data))

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.missing == (DataKind.CONTRACTS,)
    assert session.contracts == ContractNeeds(needed=2, missing=1)


def test_a_contract_that_never_traded_is_stored_and_does_not_count_as_missing() -> None:
    # Captured and empty is a fact: the contract was listed and did not trade.
    never_traded = Day(underlying=steady("500.20"), options={CALL_500: {}})

    session = _only(_check({WED: never_traded}))

    assert session.verdict is SessionVerdict.READY
    assert session.contracts == ContractNeeds(needed=1, missing=0)


def test_a_window_that_does_not_fit_a_half_day_is_ready_needs_nothing_and_says_which_rule_skips_it() -> None:
    late = setup(
        entry={"anchor": "open", "offset_ms": 205 * MS_PER_MINUTE},
        exit={"anchor": "close", "offset_ms": 10 * MS_PER_MINUTE},
        legs=[leg(expiry={"kind": "same_day"})],
    )
    # Nothing at all is stored for the half day, and a full day is stored whole.
    data = CheckData(fake_data({THU: _stored()}))

    checked = check_data(late, [THU, HALF_DAY], now_ms=NOW_MS + 60 * 24 * 3600 * 1000, data=data)

    full, half = checked.sessions
    assert full.verdict is SessionVerdict.READY and full.skip is None
    assert half.verdict is SessionVerdict.READY
    assert half.skip is SkipReason.WINDOW_DOES_NOT_FIT
    assert half.contracts == ContractNeeds(0, 0) and half.missing == () and half.problems == ()
    assert ("underlying", HALF_DAY) not in data.asked, "a skipped session reads nothing"


def test_a_session_the_underlying_did_not_trade_in_at_the_entry_time_is_ready_and_skipped() -> None:
    # The entry is read on the bar closing at minute 5. This underlying has no bar for minute 5.
    without_the_entry_minute = {minute: "500.20" for minute in range(1, 391) if minute != 5}
    day = Day(underlying=without_the_entry_minute, options={})

    session = _only(_check({WED: day}))

    assert session.verdict is SessionVerdict.READY
    assert session.skip is SkipReason.UNDERLYING_DID_NOT_TRADE
    assert session.contracts == ContractNeeds(0, 0)


def test_a_session_the_underlying_did_not_trade_in_still_needs_its_listing_because_the_run_reads_it() -> None:
    without_the_entry_minute = {minute: "500.20" for minute in range(1, 391) if minute != 5}
    day = Day(underlying=without_the_entry_minute, options={})

    session = _only(_check({WED: day}, without_listing=(WED,)))

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.missing == (DataKind.LISTING,)


def test_a_session_older_than_the_data_plan_cannot_be_tested_and_the_floor_day_itself_can() -> None:
    friday_before_the_floor = date(2024, 10, 11)
    the_floor_day = date(2024, 10, 14)
    data = CheckData(FakeRunData())
    free = setup(legs=[leg(expiry={"kind": "same_day"})])

    checked = check_data(free, [friday_before_the_floor, the_floor_day], now_ms=NOW_MS, data=data)

    too_old, at_the_floor = checked.sessions
    assert too_old.verdict is SessionVerdict.CANNOT_BE_TESTED
    assert [problem.code for problem in too_old.problems] == [ProblemCode.OLDER_THAN_THE_DATA_PLAN]
    assert at_the_floor.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert checked.option_prices_from == date(2024, 10, 14)
    assert ("underlying", friday_before_the_floor) not in data.asked, "a session that cannot be tested is not read"


def test_a_session_that_is_not_over_cannot_be_tested_and_says_its_data_is_not_final() -> None:
    inside_friday = session_window_for_date(FRI).open_ms_utc + 60 * MS_PER_MINUTE

    checked = _check({THU: _stored(), FRI: _stored()}, now_ms=inside_friday)

    thursday, friday = checked.sessions
    assert thursday.verdict is SessionVerdict.READY
    assert friday.verdict is SessionVerdict.CANNOT_BE_TESTED
    [problem] = friday.problems
    assert problem.code is ProblemCode.SESSION_DATA_NOT_FINAL
    assert "not final" in problem.message and "next New York day" in problem.message


def test_estimated_fees_on_a_date_without_published_rates_cannot_be_tested_even_when_the_window_skips_it() -> None:
    # The fee model has no rates before July 2026, so a run that estimates fees refuses such a date outright,
    # whether the session would have traded or been skipped.
    estimated = setup(
        fees={"kind": "estimated"},
        entry={"anchor": "open", "offset_ms": 205 * MS_PER_MINUTE},
        exit={"anchor": "close", "offset_ms": 10 * MS_PER_MINUTE},
        legs=[leg(expiry={"kind": "same_day"})],
    )
    before_the_rates_on_a_half_day = date(2025, 11, 28)
    priced = date(2026, 7, 1)
    data = CheckData(fake_data({priced: _stored()}))

    checked = check_data(estimated, [before_the_rates_on_a_half_day, priced], now_ms=NOW_MS, data=data)

    unpriced, with_rates = checked.sessions
    assert unpriced.verdict is SessionVerdict.CANNOT_BE_TESTED
    assert [problem.code for problem in unpriced.problems] == [ProblemCode.NO_PUBLISHED_FEE_RATES]
    assert with_rates.verdict is SessionVerdict.READY


def test_a_fee_per_contract_or_none_is_never_a_reason_to_refuse_a_date() -> None:
    old_but_in_the_plan = date(2025, 6, 13)
    for fees in ({"kind": "not_charged"}, {"kind": "per_contract", "per_contract_usd": "0.65"}):
        data = CheckData(fake_data({old_but_in_the_plan: _stored()}))
        checked = check_data(
            setup(fees=fees, legs=[leg(expiry={"kind": "same_day"})]), [old_but_in_the_plan], now_ms=NOW_MS, data=data
        )

        assert _only(checked).verdict is SessionVerdict.READY


def test_a_session_can_have_more_than_one_reason_and_all_are_given() -> None:
    estimated = setup(fees={"kind": "estimated"}, legs=[leg(expiry={"kind": "same_day"})])
    old = date(2024, 10, 11)

    checked = check_data(estimated, [old], now_ms=NOW_MS, data=CheckData(FakeRunData()))

    assert {problem.code for problem in _only(checked).problems} == {
        ProblemCode.NO_PUBLISHED_FEE_RATES,
        ProblemCode.OLDER_THAN_THE_DATA_PLAN,
    }


# ── What the lake wrote down ─────────────────────────────────────────────


def _failed(reason: str, *, again: bool) -> RecordedFailure:
    return RecordedFailure(reason=reason, will_be_asked_again=again)


def test_a_failure_the_lake_will_not_try_again_cannot_be_tested_and_gives_the_lakes_reason() -> None:
    failures = {(DataKind.LISTING, WED, None): _failed("provider_entitlement_error", again=False)}

    session = _only(_check({WED: _stored()}, without_listing=(WED,), failures=failures))

    assert session.verdict is SessionVerdict.CANNOT_BE_TESTED
    [problem] = session.problems
    assert problem.code is ProblemCode.LAKE_HOLDS_A_FAILURE
    assert problem.lake_reason == "provider_entitlement_error"
    assert "listing" in problem.message


def test_a_failure_the_lake_will_try_again_is_a_download_with_a_note_and_not_a_refusal() -> None:
    failures = {(DataKind.UNDERLYING_BARS, WED, None): _failed("provider_rate_limited", again=True)}

    session = _only(_check({WED: _stored()}, without_underlying=(WED,), failures=failures))

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.problems == ()
    assert any("provider_rate_limited" in note and "again" in note for note in session.notes)


def test_a_contract_the_lake_gave_up_on_makes_the_session_untestable_naming_that_contract() -> None:
    call = contract_on(WED, "call", 500)
    failures = {(DataKind.CONTRACTS, WED, call.occ_symbol): _failed("fetch_timeout", again=False)}
    with_the_call_missing = Day(underlying=steady("500.20"), options={})

    session = _only(_check({WED: with_the_call_missing}, failures=failures))

    assert session.verdict is SessionVerdict.CANNOT_BE_TESTED
    assert call.occ_symbol in session.problems[0].message
    assert session.contracts == ContractNeeds(needed=1, missing=1)


def test_a_failure_recorded_for_something_that_is_stored_is_not_looked_at() -> None:
    failures = {(DataKind.LISTING, WED, None): _failed("fetch_timeout", again=False)}

    session = _only(_check({WED: _stored()}, failures=failures))

    assert session.verdict is SessionVerdict.READY


# ── Stored files that do not read ────────────────────────────────────────


def test_a_stored_contract_day_that_does_not_read_is_counted_missing_with_a_note() -> None:
    call = contract_on(WED, "call", 500)

    session = _only(_check({WED: _stored()}, damaged=((WED, call.occ_symbol),)))

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.contracts == ContractNeeds(needed=1, missing=1)
    assert any(call.occ_symbol in note and "fetched again" in note for note in session.notes)


def _the_underlying_with_a_minute_twice(*, replaced: bool) -> SessionCheck:
    data = fake_data({WED: _stored()})
    bars = list(data._underlying[WED])
    data._underlying[WED] = (*bars, bars[-1])  # the same minute twice
    return _only(
        check_data(
            setup(legs=[leg(expiry={"kind": "same_day"})]),
            [WED],
            now_ms=NOW_MS,
            data=CheckData(data, download_replaces_the_underlying=replaced),
        )
    )


def test_stored_underlying_bars_that_break_the_rules_of_a_bar_and_that_a_download_keeps_cannot_be_tested() -> None:
    session = _the_underlying_with_a_minute_twice(replaced=False)

    assert session.verdict is SessionVerdict.CANNOT_BE_TESTED
    assert [problem.code for problem in session.problems] == [ProblemCode.STORED_DATA_IS_MALFORMED]
    [problem] = session.problems
    assert "the underlying" in problem.message and "break the rules of a bar" in problem.message
    assert_plain(problem.message)
    assert session.notes == ()


def test_stored_underlying_bars_that_break_the_rules_of_a_bar_and_that_a_download_replaces_need_a_download() -> None:
    session = _the_underlying_with_a_minute_twice(replaced=True)

    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert (session.missing, session.problems) == ((DataKind.UNDERLYING_BARS,), ())
    assert session.contracts == ContractNeeds(needed=None, missing=None)  # the contracts are not known without the day
    assert any("underlying bars" in note and "will be fetched again" in note for note in session.notes)


def test_stored_option_bars_that_break_the_rules_of_a_bar_cannot_be_tested_and_name_the_contract_but_no_date() -> None:
    call = contract_on(WED, "call", 500)
    data = fake_data({WED: _stored()})
    bars = list(data._options[(call, WED)])
    data._options[(call, WED)] = (*bars, bars[-1])  # the same minute twice

    session = _only(
        check_data(setup(legs=[leg(expiry={"kind": "same_day"})]), [WED], now_ms=NOW_MS, data=CheckData(data))
    )

    assert session.verdict is SessionVerdict.CANNOT_BE_TESTED
    [problem] = session.problems
    assert problem.code is ProblemCode.STORED_DATA_IS_MALFORMED
    assert call.occ_symbol in problem.message
    assert_plain(problem.message)


# ── The testable range ───────────────────────────────────────────────────


def _session(day: date, verdict: SessionVerdict) -> SessionCheck:
    window = session_window_for_date(day)
    return SessionCheck(
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        verdict=verdict,
        skip=None,
        missing=(),
        contracts=ContractNeeds(0, 0),
        problems=(),
        notes=(),
    )


def _range(*verdicts: SessionVerdict) -> DataCheck:
    # Seven consecutive weekdays from Monday 2026-10-05 would include a weekend; these are only labelled sessions.
    days = [date(2026, 10, 5), date(2026, 10, 6), WED, THU, FRI, date(2026, 10, 12), date(2026, 10, 13)]
    return DataCheck(
        tuple(_session(day, verdict) for day, verdict in zip(days, verdicts, strict=False)), date(2024, 10, 14)
    )


R, N, C = SessionVerdict.READY, SessionVerdict.NEEDS_DOWNLOAD, SessionVerdict.CANNOT_BE_TESTED


def test_the_first_and_last_session_that_can_be_tested_skip_those_that_cannot() -> None:
    checked = _range(C, R, N, C, R, C, C)

    assert checked.first_testable is not None
    assert checked.first_testable.session_open_ms == _open(date(2026, 10, 6))
    assert checked.last_testable is not None and checked.last_testable.session_open_ms == _open(FRI)


def test_the_longest_unbroken_run_of_testable_sessions_is_offered_as_a_range() -> None:
    # Indices 1 to 3 are three testable sessions in a row (Tue, Wed, Thu); 5 to 6 are two. The run is Tue to Thu.
    checked = _range(C, R, N, R, C, R, R)

    stretch = checked.longest_testable_stretch

    assert stretch is not None
    assert (stretch.first_session_open_ms, stretch.last_session_open_ms, stretch.sessions) == (
        _open(date(2026, 10, 6)),
        _open(THU),
        3,
    )


def test_of_two_runs_as_long_the_earlier_is_offered() -> None:
    checked = _range(R, R, C, R, R, C, C)

    stretch = checked.longest_testable_stretch

    assert stretch is not None and (stretch.first_session_open_ms, stretch.sessions) == (_open(date(2026, 10, 5)), 2)


def test_a_run_that_ends_the_range_is_counted() -> None:
    checked = _range(C, R, C, R, R, R, N)

    stretch = checked.longest_testable_stretch

    assert stretch is not None
    assert (stretch.first_session_open_ms, stretch.last_session_open_ms, stretch.sessions) == (
        _open(THU),
        _open(date(2026, 10, 13)),
        4,
    )


def test_a_range_with_nothing_testable_offers_no_range() -> None:
    checked = _range(C, C, C)

    assert checked.longest_testable_stretch is None
    assert checked.first_testable is None and checked.last_testable is None


def test_a_session_that_cannot_be_tested_breaks_a_run_of_sessions_even_when_it_is_one_in_a_stored_range() -> None:
    # End to end: Wednesday and Friday are stored, Thursday's listing is missing for good.
    failures = {(DataKind.LISTING, THU, None): _failed("fetch_timeout", again=False)}

    checked = _check(
        {WED: _stored(), THU: _stored(), FRI: _stored()},
        sessions=[WED, THU, FRI],
        without_listing=(THU,),
        failures=failures,
    )

    assert [session.verdict for session in checked.sessions] == [R, C, R]
    stretch = checked.longest_testable_stretch
    assert stretch is not None and (stretch.first_session_open_ms, stretch.sessions) == (_open(WED), 1)


# ── What the words say ───────────────────────────────────────────────────


def _every_code(annotation: Any) -> set[str]:
    codes: set[str] = set()
    for argument in get_args(annotation):
        codes |= {argument} if isinstance(argument, str) else _every_code(argument)
    return codes


def test_every_reason_the_lake_or_the_download_can_give_has_plain_words_without_a_date_json_or_path() -> None:
    codes = (
        _every_code(ArtifactFailureReason)
        | _every_code(OptionFailureReason)
        | {
            fetching.UNDERLYING_NOT_FETCHED,
            fetching.NOT_IN_THE_LAKE,
            fetching.STILL_BEING_FETCHED,
            fetching.TOOK_TOO_LONG,
        }
    )

    assert codes - set(LAKE_REASON_WORDS) == set(), "a reason with no words of its own"
    for code in sorted(codes):
        assert_plain(lake_reason_words(code))
    assert_plain(lake_reason_words("a_reason_the_lake_will_have_in_future"))


def test_a_failure_the_lake_recorded_is_told_in_its_reason_code_and_a_plain_phrase_and_never_in_the_lakes_text() -> None:
    failures = {(DataKind.LISTING, WED, None): _failed("provider_rate_limited", again=False)}

    session = _only(_check({WED: _stored()}, without_listing=(WED,), failures=failures))

    [problem] = session.problems
    assert "asked us to slow down" in problem.message and "(provider_rate_limited)" in problem.message
    assert_plain(problem.message)


def test_a_failure_the_lake_will_try_again_is_told_the_same_way() -> None:
    failures = {(DataKind.UNDERLYING_BARS, WED, None): _failed("provider_api_error", again=True)}

    session = _only(_check({WED: _stored()}, without_underlying=(WED,), failures=failures))

    [note] = session.notes
    assert "did not answer properly" in note and "(provider_api_error)" in note
    assert_plain(note)


def test_no_sentence_of_a_check_names_a_date() -> None:
    estimated = setup(fees={"kind": "estimated"}, legs=[leg(expiry={"kind": "same_day"})])
    old = date(2024, 10, 11)
    inside_friday = session_window_for_date(FRI).open_ms_utc + 60 * MS_PER_MINUTE
    call = contract_on(WED, "call", 500)
    scenes = [
        check_data(estimated, [old], now_ms=NOW_MS, data=CheckData(FakeRunData())),
        _check({FRI: _stored()}, now_ms=inside_friday),
        _check({WED: _stored()}, damaged=((WED, call.occ_symbol),)),
        _check(
            {WED: Day(underlying=steady("500.20"), options={})},
            failures={(DataKind.CONTRACTS, WED, call.occ_symbol): _failed("fetch_timeout", again=False)},
        ),
    ]

    sentences = [
        text
        for checked in scenes
        for session in checked.sessions
        for text in (*(problem.message for problem in session.problems), *session.notes)
    ]

    assert len(sentences) >= 5
    for sentence in sentences:
        assert_plain(sentence)


# ── Progress and stopping ────────────────────────────────────────────────


def test_each_session_is_reported_as_it_is_checked_and_a_stop_is_asked_before_each() -> None:
    reported: list[tuple[int, int]] = []
    asked: list[int] = []
    days = {WED: _stored(), THU: _stored(), FRI: _stored()}

    check_data(
        setup(legs=[leg(expiry={"kind": "same_day"})]),
        [WED, THU, FRI],
        now_ms=NOW_MS,
        data=CheckData(fake_data(days)),
        on_session=lambda done, total: reported.append((done, total)),
        should_stop=lambda: bool(asked.append(len(reported))),
    )

    assert reported == [(1, 3), (2, 3), (3, 3)]
    assert asked == [0, 1, 2]


def test_a_stop_ends_the_check_before_the_next_session_is_read() -> None:
    data = CheckData(fake_data({WED: _stored(), THU: _stored(), FRI: _stored()}))
    reported: list[int] = []

    with pytest.raises(CheckStopped) as caught:
        check_data(
            setup(legs=[leg(expiry={"kind": "same_day"})]),
            [WED, THU, FRI],
            now_ms=NOW_MS,
            data=data,
            on_session=lambda done, _total: reported.append(done),
            should_stop=lambda: len(reported) >= 2,
        )

    assert caught.value.sessions_done == 2
    assert ("underlying", FRI) not in data.asked


class _SlowData(CheckData):
    """A lake that takes ten seconds of fake time to read each session, as a catalog with a receipt lookup a file does."""

    def __init__(self, base: FakeRunData, clock: list[float]) -> None:
        super().__init__(base)
        self._clock = clock

    def underlying_day(self, trading_date: date) -> Any:
        self._clock[0] += 10.0
        return super().underlying_day(trading_date)


def test_a_check_that_takes_ten_minutes_asks_whether_to_stop_at_least_every_thirty_seconds() -> None:
    # Seventy sessions at ten seconds each is over ten minutes. The job's lease lasts five.
    sessions = list(expected_sessions(date(2026, 7, 1), date(2026, 10, 9)))[:70]
    clock = [0.0]
    polled_at: list[float] = []
    data = _SlowData(fake_data({}), clock)

    def should_stop() -> bool:
        polled_at.append(clock[0])
        return False

    check_data(
        setup(legs=[leg(expiry={"kind": "same_day"})]),
        sessions,
        now_ms=NOW_MS,
        data=data,
        should_stop=should_stop,
    )

    marks = [0.0, *polled_at, clock[0]]
    longest = max(later - earlier for earlier, later in pairwise(marks))
    assert clock[0] >= 600, "the check took its ten minutes of fake time"
    assert len(polled_at) == len(sessions)
    assert longest <= 30
