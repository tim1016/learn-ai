"""Every Signal Program's decision clock must match the bars it will be fed.

``SignalSession.advance`` rejects any consolidated bar whose width is not
exactly the session's timeframe. That timeframe used to be a fixed
15-minute class constant, so a program deployed at any other cadence
quarantined every decision clock as ``TIMEFRAME_MISMATCH`` -- the bot ran,
consumed bars, and silently never decided. It produced no error and no
intent, which is the hardest shape of failure to notice in production.

Only ``ema_crossover_signal`` happened to run at 15 minutes. Every other
program promoted in issue #1730 either exposes a deploy-time
``resolution_minutes`` or runs on a fixed non-15-minute cadence, so this
is driven off the registry rather than a hand-listed set of keys: a
program added in a later wave is covered the moment it is registered.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import EvaluationMode, StageQuarantine

_RESOLUTIONS_TO_PROVE = (1, 5, 15, 30)


def _bar_of_width(symbol: str, width_ms: int) -> TradeBar:
    return TradeBar(
        symbol=symbol,
        start_ms=0,
        end_ms=width_ms,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=100,
    )


def _width_is_accepted(program: object, bar: TradeBar) -> bool:
    """Whether the session's width gate admits this bar.

    The gate runs before ``evaluate_signal_bar``, so reaching the strategy
    at all proves the width was accepted. An uninitialised indicator raises
    from inside the strategy, which is a pass for this narrow question --
    the alternative would be standing up a full engine context per case
    just to re-answer something the gate already decided.
    """
    try:
        stage = program.session.advance(bar, mode=EvaluationMode.DECIDE)  # type: ignore[attr-defined]
    except AssertionError:
        return True
    return not (isinstance(stage, StageQuarantine) and stage.reason == "TIMEFRAME_MISMATCH")


def _sealed_programs() -> list[tuple[str, object]]:
    return [(key, reg) for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]


def test_every_signal_program_accepts_bars_at_its_registered_default_cadence() -> None:
    programs = _sealed_programs()
    assert programs, "expected at least one registered Signal Program"

    for key, reg in programs:
        params = reg.param_schema()  # type: ignore[attr-defined]
        program = reg.signal_program_factory(params)  # type: ignore[attr-defined]
        width = program.session.timeframe_ms
        assert _width_is_accepted(program, _bar_of_width(params.symbol, width)), (
            f"'{key}' rejects a bar at its own session timeframe ({width // 60_000}min)"
        )


def test_configurable_resolution_programs_track_the_resolved_decision_clock() -> None:
    """A program exposing ``resolution_minutes`` must build a session whose
    clock follows the resolved parameter, not the contract's validated
    value. Regression for the 15-minute class constant: ``sma_crossover``
    at ``resolution_minutes=5`` previously returned
    ``StageQuarantine(REJECTED_BAR, "TIMEFRAME_MISMATCH")`` for every bar.
    """
    configurable = [
        (key, reg)
        for key, reg in _sealed_programs()
        if "resolution_minutes" in reg.param_schema.model_fields  # type: ignore[attr-defined]
    ]
    assert configurable, "expected at least one resolution-configurable Signal Program"

    for key, reg in configurable:
        for minutes in _RESOLUTIONS_TO_PROVE:
            params = reg.param_schema(resolution_minutes=minutes)  # type: ignore[attr-defined]
            program = reg.signal_program_factory(params)  # type: ignore[attr-defined]

            assert program.session.timeframe_ms == minutes * 60_000, (
                f"'{key}' built a {program.session.timeframe_ms // 60_000}min decision clock "
                f"for resolution_minutes={minutes}"
            )
            assert _width_is_accepted(program, _bar_of_width(params.symbol, minutes * 60_000)), (
                f"'{key}' at resolution_minutes={minutes} rejects its own bars -- "
                "it would consume bars and never decide"
            )


def test_quarantined_decision_bar_is_counted_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A refused bar must never vanish silently.

    ``on_consolidated_bar`` used to drop a ``StageQuarantine`` with no branch,
    no log and no counter. The session refused the bar, the runner never
    learned of it, and the bot kept consuming data while producing zero
    decisions -- "running but deciding nothing", which is the hardest live
    failure to notice. A mis-shaped bucket is an anomaly, never routine.
    """
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    program = registration.signal_program_factory(registration.param_schema())
    program.activate_for_backtest()
    width = program.session.timeframe_ms

    # A bucket one minute short of the session's own cadence.
    mis_shaped = TradeBar(
        symbol="SPY",
        start_ms=0,
        end_ms=width - 60_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1_000,
    )

    with caplog.at_level(logging.WARNING, logger="app.engine.strategy.signal_program"):
        program.on_consolidated_bar(mis_shaped)

    assert program.quarantine_counts == {"TIMEFRAME_MISMATCH": 1}
    assert program.take_completed_stage() is None
    assert any("quarantined" in record.message.lower() for record in caplog.records), (
        "a quarantined decision bar produced no warning; it would be invisible in production"
    )
