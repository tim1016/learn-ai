"""Research is the canonical replay once per session, without silent ladders or phantom cash."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.run import RunStopped, run_sessions
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.protocol import Window
from tests.options_backtest._run_data import FRI, THU, WED, open_ms
from tests.research.options_golden_search._evidence import scene

D = Decimal


def _window(days: Sequence[date]) -> Window:
    return Window(
        key="development", start_ms=open_ms(days[0]),
        end_ms=session_window_for_date(days[-1]).close_ms_utc + 1,
        session_ms=tuple(open_ms(day) for day in days), annualization_sessions=251, activity_floor=1,
    )


def test_lean_result_and_pins_match_ordinary_backtest_exactly_with_chronological_cash() -> None:
    setup, data = scene((WED, THU, FRI), exits=("0.5", "3", "1"))
    ordinary = run_sessions(setup, (WED, THU, FRI), data)
    lean = evaluate_setup(setup, _window((WED, THU, FRI)), data)
    assert [session.outcome for session in lean.sessions] == [session.outcome for session in ordinary.sessions]
    assert [session.outcome.money_steps for session in lean.sessions] == [session.outcome.money_steps for session in ordinary.sessions]
    assert lean.pins == tuple(session.pins for session in ordinary.sessions)
    assert [session.cash_before_usd for session in lean.sessions] == [D("10000"), D("9950"), D("10150")]
    assert lean.metrics.net_profit_usd == ordinary.summary.net_profit_usd == 150
    assert lean.metrics.completed_traded_sessions == 3
    assert not lean.metrics.technical_failures


def test_one_canonical_replay_per_day_and_no_implicit_ladders(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.research.options_golden_search.evaluator as evaluator

    setup, data = scene((WED, THU), exits=("2", "3"))
    original = evaluator.replay_session
    calls = []

    def record(*args: object, **kwargs: object) -> object:
        calls.append(kwargs["cash_usd"])
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluator, "replay_session", record)
    evaluate_setup(setup, _window((WED, THU)), data)
    assert calls == [D("10000"), D("10100")]
    assert len(data.asked) == 6  # underlying, listing, one reachable option per day


@pytest.mark.parametrize("missing_minute", [7, 61])
def test_missing_held_mark_or_marked_session_stops_before_reading_or_funding_next_day(missing_minute: int) -> None:
    setup, data = scene((WED, THU), exits=("2", "3"), missing_minute=missing_minute)
    result = evaluate_setup(setup, _window((WED, THU)), data)
    assert result.scheduled_sessions == 2
    assert len(result.sessions) == 1
    assert result.metrics.technical_failures
    assert result.metrics.missing_held_marks >= 1
    assert all(call[1] == WED for call in data.asked)
    if missing_minute == 7:
        assert result.metrics.net_profit_usd == 100
    else:
        assert any("incomplete_session" in reason for reason in result.metrics.technical_failures)


@pytest.mark.parametrize("fee_policy", [{"kind": "per_contract", "per_contract_usd": "1"}, {"kind": "estimated"}])
def test_fee_stress_scales_canonical_per_fill_policy_output_including_estimated_tiers(fee_policy: dict[str, str]) -> None:
    setup, data = scene((FRI,), exits=("2",))
    setup = OptionsBacktestSetup.model_validate({**setup.model_dump(mode="python"), "fees": fee_policy})
    base = evaluate_setup(setup, _window((FRI,)), data)
    stressed = evaluate_setup(setup, _window((FRI,)), data, fee_multiplier=D(2))
    base_trade, stressed_trade = base.sessions[0].outcome.trades[0], stressed.sessions[0].outcome.trades[0]
    assert stressed_trade.entry_fee_usd == 2 * base_trade.entry_fee_usd
    assert stressed_trade.exit_fee_usd == 2 * base_trade.exit_fee_usd
    assert stressed.metrics.net_profit_usd == base.metrics.net_profit_usd - base_trade.entry_fee_usd - base_trade.exit_fee_usd
    assert not stressed.metrics.technical_failures


def test_unfunded_close_stops_qualification_and_preserves_unclamped_loss() -> None:
    setup, data = scene((WED, THU), exits=("0.001", "3"))
    setup = OptionsBacktestSetup.model_validate({
        **setup.model_dump(mode="python"), "starting_cash_usd": "200",
        "fees": {"kind": "per_contract", "per_contract_usd": "100"},
    })
    result = evaluate_setup(setup, _window((WED, THU)), data)
    assert len(result.sessions) == 1
    assert result.metrics.net_profit_usd == -300
    assert result.metrics.max_drawdown_fraction == D("1.5")
    assert result.metrics.capital_evidence.ended_by.kind.value == "unfunded_close"
    assert any("capital_invalid" in reason for reason in result.metrics.technical_failures)
    assert all(call[1] == WED for call in data.asked)


def test_cancellation_during_observation_never_returns_a_partial_scientific_result() -> None:
    setup, data = scene((WED, THU), exits=("2", "3"))
    checks = iter((False, True))
    with pytest.raises(RunStopped) as error:
        evaluate_setup(setup, _window((WED, THU)), data, should_stop=lambda: next(checks))
    assert error.value.sessions_done == 0


def test_progress_reports_real_observed_minutes_monotonically_across_sessions() -> None:
    setup, data = scene((WED, THU), exits=("2", "3"))
    progress: list[int] = []
    result = evaluate_setup(setup, _window((WED, THU)), data, on_progress=progress.append)
    assert progress == list(range(1, 783))
    assert progress[-1] == sum(len(session.observations) for session in result.sessions)
