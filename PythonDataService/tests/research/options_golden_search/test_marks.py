"""Hand-worked held-minute mark rules; no expectation is taken from marks.py (ADR 0080, #3405).

Every dollar below is worked by hand from the bars: equity = cash + Σ(signed contracts × multiplier × mark).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.observation import HeldMark, MinuteObservation
from app.options_backtest.run import run_sessions
from app.research.options_golden_search import stages
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.marks import MARK_RULE_V1, MARK_RULE_V2, value_session
from app.research.options_golden_search.metrics import evidence_from_json, summarize_evidence
from app.research.options_golden_search.projections import exact_data
from app.research.options_golden_search.protocol import OptionsSearchProtocol, Window
from app.research.runs.hashing import hash_payload
from tests.options_backtest._market import at, contract, leg, setup
from tests.options_backtest._run_data import THU, WED, Day, fake_data, open_ms
from tests.research.options_golden_search._evidence import session_evidence

D = Decimal
CALL, PUT = contract("call", "500"), contract("put", "500")
FIXTURES = Path(__file__).parents[2] / "fixtures" / "options_evidence"


def held(leg_id: str, option: OptionContract, close: str | None, signed: int = 1) -> HeldMark:
    return HeldMark(leg_id, option, signed, 100, None if close is None else D(close), False)


def minute(k: int, cash: str, *marks: HeldMark) -> MinuteObservation:
    return MinuteObservation(at(k), D(cash), D(0), marks)


def test_value_session_v1_leaves_a_missing_mark_missing() -> None:
    session = (minute(6, "9900", held("call", CALL, "1")), minute(7, "9900", held("call", CALL, None)))
    valued = value_session(session, MARK_RULE_V1)
    assert valued.observations == session
    assert valued.carried == ()
    assert valued.observations[1].equity_usd is None


def test_value_session_v2_carries_the_previous_close_never_a_later_one_and_counts_each_minute() -> None:
    session = (
        minute(6, "9900", held("call", CALL, "1")),
        minute(7, "9900", held("call", CALL, "0.6")),
        minute(8, "9900", held("call", CALL, None)),
        minute(9, "9900", held("call", CALL, None)),
        minute(10, "9900", held("call", CALL, "0.9")),
    )
    valued = value_session(session, MARK_RULE_V2)
    # 9900 + 100 x 1, then 9900 + 100 x 0.6 at minute 7 and, carried, at minutes 8 and 9; minute 10's 0.9 is never read back.
    assert [point.equity_usd for point in valued.observations] == [D("10000"), D("9960"), D("9960"), D("9960"), D("9990")]
    assert [(m.at_ms, m.leg_id, m.close, m.traded_at_ms, m.run) for m in valued.carried] == [
        (at(8), "call", D("0.6"), at(7), 1), (at(9), "call", D("0.6"), at(7), 2),
    ]
    assert valued.longest_carried_run == 2
    # The raw trace is untouched: the same evidence still reads as missing under the original rule.
    assert session[2].held[0].close is None


def test_value_session_v2_counts_two_carried_legs_twice_and_runs_per_position() -> None:
    session = (
        minute(6, "9700", held("call", CALL, "1"), held("put", PUT, "2")),
        minute(7, "9700", held("call", CALL, "0.6"), held("put", PUT, "1.5")),
        minute(8, "9700", held("call", CALL, None), held("put", PUT, "1.4")),
        minute(9, "9700", held("call", CALL, None), held("put", PUT, None)),
        minute(10, "9700", held("call", CALL, "0.9"), held("put", PUT, None)),
        minute(11, "9700", held("call", CALL, None), held("put", PUT, "1.4")),
    )
    valued = value_session(session, MARK_RULE_V2)
    assert [(m.at_ms, m.leg_id, m.run) for m in valued.carried] == [
        (at(8), "call", 1), (at(9), "call", 2), (at(9), "put", 1), (at(10), "put", 2), (at(11), "call", 1),
    ]
    assert sum(m.at_ms == at(9) for m in valued.carried) == 2, "a minute with two carried legs counts twice"
    assert valued.longest_carried_run == 2, "a real bar ends a run: call 2, put 2, call 1"
    # Minute 9: call carried at 0.6 and put carried at 1.4 -> 9700 + 60 + 140.
    assert valued.observations[3].equity_usd == D("9900")
    assert valued.observations[4].equity_usd == D("9930")


def test_value_session_v2_leaves_a_position_that_has_traded_nothing_missing() -> None:
    first_session = (minute(6, "9900", held("call", CALL, "1")), minute(7, "9900", held("call", CALL, "1")))
    next_session = (minute(6, "9900", held("call", CALL, None)), minute(7, "9900", held("call", CALL, "1")))
    assert value_session(first_session, MARK_RULE_V2).carried == ()
    fresh = value_session(next_session, MARK_RULE_V2)
    assert fresh.observations[0].missing_leg_ids == ("call",), "an earlier session never lends a price"
    assert fresh.carried == ()

    reopened = (
        minute(6, "9900", held("call", CALL, "1")),
        minute(7, "10000"),
        minute(8, "9900", held("call", CALL, None)),
    )
    again = value_session(reopened, MARK_RULE_V2)
    assert again.observations[2].missing_leg_ids == ("call",), "a closed position's price never marks a later position"


def test_value_session_v2_never_takes_another_contracts_price() -> None:
    other = contract("call", "505")
    session = (minute(6, "9900", held("call", CALL, "1")), minute(7, "9900", held("call", other, None)))
    valued = value_session(session, MARK_RULE_V2)
    assert valued.observations[1].missing_leg_ids == ("call",)
    assert valued.carried == ()


def test_value_session_v2_sets_no_cap_on_a_carried_run() -> None:
    session = (minute(6, "9900", held("call", CALL, "1")),
        *(minute(k, "9900", held("call", CALL, None)) for k in range(7, 391)))
    valued = value_session(session, MARK_RULE_V2)
    assert valued.longest_carried_run == len(valued.carried) == 384
    assert {point.equity_usd for point in valued.observations} == {D("10000")}


def test_value_session_rejects_an_unknown_rule() -> None:
    with pytest.raises(ValueError, match="unknown held-minute mark rule"):
        value_session((), "options-held-minute-close-v3")


def test_a_position_that_traded_nothing_is_still_a_technical_failure_under_v2() -> None:
    session = session_evidence(WED)
    first, later = session.observations[6], session.observations[7]
    assert first.held and later.held

    def blank(point: MinuteObservation) -> MinuteObservation:
        return replace(point, held=tuple(replace(mark, close=None) for mark in point.held))

    # Neither the first held minute nor, with no price yet, the next one has anything to carry.
    opened_blind = replace(session, observations=(*session.observations[:6], blank(first), blank(later), *session.observations[8:]))
    v2 = summarize_evidence((opened_blind,), starting_capital_usd=D("10000"), annualization_sessions=251, mark_rule=MARK_RULE_V2)
    assert v2.missing_held_marks == 2 and v2.carried_held_marks == 0
    assert [failure.split(":", 1)[1] for failure in v2.technical_failures] == ["missing_held_mark:call", "missing_held_mark:call"]
    # With the first minute traded, the second is carried and nothing fails.
    traded_then_blank = replace(session, observations=(*session.observations[:7], blank(later), *session.observations[8:]))
    carried = summarize_evidence((traded_then_blank,), starting_capital_usd=D("10000"), annualization_sessions=251, mark_rule=MARK_RULE_V2)
    assert (carried.missing_held_marks, carried.carried_held_marks, carried.technical_failures) == (0, 1, ())


def test_mark_rule_identifiers_are_the_owner_decided_spellings() -> None:
    assert (MARK_RULE_V1, MARK_RULE_V2) == ("options-held-minute-close-v1", "options-held-minute-close-v2")


# --- one canonical replay, valued under both rules ---------------------------------------------------------------


def _two_leg_days(*days: date):
    call = {**{k: ("1.00", "1.00") for k in (5, 6)}, 7: ("1.00", "0.60"), 10: ("0.60", "0.90"),
            **{k: ("0.90", "0.90") for k in range(11, 61)}, 61: ("2.00", "2.00")}
    put = {**{k: ("2.00", "2.00") for k in (5, 6)}, 7: ("2.00", "1.50"), 8: ("1.50", "1.40"),
           **{k: ("1.40", "1.40") for k in range(11, 61)}, 61: ("1.50", "1.50")}
    # Minutes 8 and 9 of the call, and 9 and 10 of the put, did not trade.
    return fake_data({day: Day(underlying={k: "500" for k in range(1, 391)},
        options={("call", 500): call, ("put", 500): put}) for day in days})


def _window(days: tuple[date, ...]) -> Window:
    return Window(key="development", start_ms=open_ms(days[0]), end_ms=session_window_for_date(days[-1]).close_ms_utc + 1,
        session_ms=tuple(open_ms(day) for day in days), annualization_sessions=251, activity_floor=1)


def test_carried_drawdown_peak_and_recovery_are_exact_over_the_carried_values_and_v1_stops() -> None:
    days = (WED,)
    written = setup(leg("call"), leg("put", "put"))
    v2 = evaluate_setup(written, _window(days), _two_leg_days(*days), mark_rule=MARK_RULE_V2)
    metrics = v2.metrics
    # Cash after the two entries is 10000 - 100 - 200 = 9700; the exit sells at 2.00 and 1.50, so cash ends at 10050.
    # Equity: minute 6 -> 10000; 7 -> 9700 + 60 + 150 = 9910; 8 -> 9700 + 60 (carried) + 140 = 9900;
    # 9 -> 9700 + 60 + 140, both carried = 9900; 10 -> 9700 + 90 + 140 (carried) = 9930; 61 -> 10050.
    assert metrics.net_profit_usd == D("50")
    assert metrics.max_drawdown_usd == D("100")
    assert metrics.max_drawdown_fraction == D("0.01")
    assert metrics.max_recovery_sessions == 1 and not metrics.ongoing_recovery
    assert [(spell.peak_at_ms, spell.recovered_at_ms, spell.below_peak_sessions) for spell in metrics.recoveries] == [
        (open_ms(WED) + 6 * 60_000, open_ms(WED) + 61 * 60_000, 1),
    ]
    assert not metrics.technical_failures
    assert (metrics.required_held_marks, metrics.missing_held_marks) == (110, 0)
    assert metrics.mark_rule == MARK_RULE_V2
    assert metrics.carried_held_marks == 4
    assert metrics.longest_carried_run == 2
    # 4 / 110 in 50 significant digits.
    assert metrics.carried_share_of_required_held_marks == D("0.0" + "36" * 25)

    # The same raw evidence under the original rule: three minutes have no equity, the drawdown skips them and the
    # marks are technical failures.
    v1 = summarize_evidence(v2.sessions, starting_capital_usd=D("10000"), annualization_sessions=251)
    assert v1.mark_rule == MARK_RULE_V1 and v1.carried_held_marks is None
    assert v1.missing_held_marks == 4
    assert v1.max_drawdown_usd == D("90")
    assert [failure.split(":", 1)[1] for failure in v1.technical_failures] == [
        "missing_held_mark:call", "missing_held_mark:call,put", "missing_held_mark:put",
    ]
    assert v1.net_profit_usd == metrics.net_profit_usd, "net profit comes from the same fills under either rule"


def test_v2_session_outcomes_and_cash_match_the_ordinary_run_and_v1_exactly() -> None:
    days = (WED, THU)
    written = setup(leg("call"), leg("put", "put"))
    ordinary = run_sessions(written, days, _two_leg_days(*days), slippage_ladder=(), cash_ladder=())
    v1 = evaluate_setup(written, _window(days), _two_leg_days(*days))
    v2 = evaluate_setup(written, _window(days), _two_leg_days(*days), mark_rule=MARK_RULE_V2)
    # v1 stops after the first session with a missing mark; v2 continues because the gap is only valued.
    assert len(v1.sessions) == 1 and len(v2.sessions) == 2 and v1.metrics.technical_failures
    assert v1.sessions[0] == v2.sessions[0], "the session replayed, observed and funded is the same"
    assert [session.outcome for session in v2.sessions] == [session.outcome for session in ordinary.sessions]
    assert [session.cash_before_usd for session in v2.sessions] == [D("10000"), D("10050")]
    assert v2.metrics.net_profit_usd == ordinary.summary.net_profit_usd == D("100")
    assert v2.metrics.carried_held_marks == 8 and v2.metrics.longest_carried_run == 2
    assert not v2.metrics.technical_failures


# --- retained real evidence -------------------------------------------------------------------------------------


def test_the_real_retained_gaps_are_carried_at_the_previous_close_and_every_original_figure_is_unchanged() -> None:
    data = json.loads((FIXTURES / "development-evidence.json").read_text())
    sessions = tuple(evidence_from_json(item) for item in data["payload"]["sessions"])
    capital = D("10000")
    original = summarize_evidence(sessions, starting_capital_usd=capital, annualization_sessions=251)
    v2 = summarize_evidence(sessions, starting_capital_usd=capital, annualization_sessions=251, mark_rule=MARK_RULE_V2)
    assert (original.required_held_marks, original.missing_held_marks) == (599, 2)
    assert (v2.required_held_marks, v2.missing_held_marks, v2.carried_held_marks, v2.longest_carried_run) == (599, 0, 2, 1)
    assert not [item for item in v2.technical_failures if "missing_held_mark" in item]
    assert v2.net_profit_usd == original.net_profit_usd == D("-339.60")
    # The put's closes in the retained trace are 0.02 at 1730925420000 and 0.03 at 1730925540000, the minutes
    # before its two gaps at 1730925480000 and 1730925600000.
    carried = value_session(sessions[-1].observations, MARK_RULE_V2)
    assert [(mark.at_ms, mark.close, mark.traded_at_ms) for mark in carried.carried] == [
        (1730925480000, D("0.02"), 1730925420000), (1730925600000, D("0.03"), 1730925540000),
    ]
    by_time = {point.at_ms: point for point in carried.observations}
    for at_ms, close in ((1730925480000, "0.02"), (1730925600000, "0.03")):
        point = by_time[at_ms]
        assert point.equity_usd == point.cash_usd + 100 * D(close)


def test_the_default_valuation_reproduces_the_retained_metrics_identity_bit_for_bit() -> None:
    row = json.loads((FIXTURES / "study-record.json").read_text())
    data = json.loads((FIXTURES / "development-evidence.json").read_text())
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    sessions = tuple(evidence_from_json(item) for item in data["payload"]["sessions"])
    metrics = summarize_evidence(sessions, starting_capital_usd=protocol.setup.starting_cash_usd,
        annualization_sessions=protocol.development.annualization_sessions,
        risk_free_daily_return=protocol.policy.risk_free_daily_return)
    stored = data["record"]["result"]
    assert hash_payload(exact_data(metrics)) == stored["metrics_sha256"]
    assert stages.metric_summary(metrics) == stored["metrics"]
    assert metrics.mark_rule == MARK_RULE_V1


def test_only_a_v2_result_names_its_rule_and_its_carried_marks_in_the_exact_identity() -> None:
    session = session_evidence(WED)
    v1 = summarize_evidence((session,), starting_capital_usd=D("10000"), annualization_sessions=251)
    v2 = summarize_evidence((session,), starting_capital_usd=D("10000"), annualization_sessions=251, mark_rule=MARK_RULE_V2)
    disclosure = {"mark_rule", "carried_held_marks", "carried_share_of_required_held_marks", "longest_carried_run"}
    assert not disclosure & set(exact_data(v1))
    assert not disclosure & set(stages.metric_summary(v1))
    assert {name: exact_data(v2)[name] for name in disclosure} == {
        "mark_rule": MARK_RULE_V2, "carried_held_marks": 0, "carried_share_of_required_held_marks": "0", "longest_carried_run": 0,
    }, "a v2 result with nothing carried still says so"
    assert disclosure <= set(stages.metric_summary(v2))
    assert hash_payload(exact_data(v1)) != hash_payload(exact_data(v2))
