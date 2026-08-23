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
from app.engine.strategy.signal_program import (
    EvaluationMode,
    SignalProgram,
    StageQuarantine,
    StageStatus,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy import (
    _QUARANTINE_LOG_EVERY,
    _build_signal_strategy,
    _LiveSignalRuntime,
)
from tests._helpers.signal_program import bind_strategy_context, bucket, sealed_programs

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


def test_every_signal_program_accepts_bars_at_its_registered_default_cadence() -> None:
    programs = sealed_programs()
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
        for key, reg in sealed_programs()
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


# ---------------------------------------------------------------------------
# A refused decision bar must never vanish silently.
#
# ``on_consolidated_bar`` used to drop a ``StageQuarantine`` with no branch at
# all: the session refused the bar, the runner never learned of it, and the
# bot kept consuming data while producing zero decisions -- "running but
# deciding nothing", the hardest live failure to notice.
#
# The surfacing is deliberately split across the sealed/unsealed boundary.
# ``app/engine/strategy/signal_program.py`` is listed in every registered
# program's ``artifact_paths``, so its bytes ARE the sealed decision identity:
# it only *retains* each refusal for its owner to drain. The counting, the
# throttling and the log line live in ``app.services.bot_trade_strategy``,
# which already owns the runner's operator surface and is not hashed. Both
# halves are proven below, because either one alone is silence.
# ---------------------------------------------------------------------------

_QUARANTINE_KEY = "sma_crossover"
_QUARANTINE_SYMBOL = "SPY"
_SEALED_LOGGER = "app.engine.strategy.signal_program"
_RUNNER_LOGGER = "app.services.bot_trade_strategy"
# Arbitrary well-formed epoch anchor; only the relative order matters here.
_BASE_MS = 1_700_000_000_000


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="sid-decision-clock",
        strategy_key=_QUARANTINE_KEY,
        broker="alpaca",
        symbol=_QUARANTINE_SYMBOL,
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan(_QUARANTINE_SYMBOL),
        run_id="run-decision-clock-1",
        created_at_ms=0,
    )


def _mis_shaped(width_ms: int, index: int = 0) -> TradeBar:
    """A bucket one minute short of the session's own cadence."""
    start = _BASE_MS + index * width_ms
    return bucket(_QUARANTINE_SYMBOL, start, start + width_ms - 60_000, "100")


def _runner_runtime() -> _LiveSignalRuntime:
    """The real live composition: ``_build_signal_strategy`` and nothing else."""
    runtime = _build_signal_strategy(_QUARANTINE_KEY, _QUARANTINE_SYMBOL, None)
    bind_strategy_context(runtime.strategy)
    return runtime


def _feed(program: SignalProgram, bar: TradeBar) -> None:
    """Drive one bucket through the production entrypoint the runner uses."""
    program.capture_source_bar(bar, mode=EvaluationMode.DECIDE)
    program.on_consolidated_bar(bar)


def _quarantine_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_refused_decision_bar_is_retained_by_the_program_and_not_logged_from_sealed_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sealed half: the refusal survives the call, and nothing is logged
    from the file whose bytes are the program's decision identity."""
    registration = _STRATEGY_REGISTRY[_QUARANTINE_KEY]
    program = registration.signal_program_factory(registration.param_schema())
    program.activate_for_backtest()
    mis_shaped = _mis_shaped(program.session.timeframe_ms)

    with caplog.at_level(logging.DEBUG, logger=_SEALED_LOGGER):
        program.on_consolidated_bar(mis_shaped)

    assert program.take_completed_stage() is None, "a refused bar must not produce a settled stage"
    quarantine = program.take_quarantine()
    assert quarantine == StageQuarantine(
        status=StageStatus.REJECTED_BAR,
        reason="TIMEFRAME_MISMATCH",
        bar_start_ms=mis_shaped.start_ms,
        bar_end_ms=mis_shaped.end_ms,
    ), f"the refused bucket was not retained for its owner to drain: {quarantine}"
    assert program.take_quarantine() is None, "the same refusal was drained twice"
    assert not caplog.records, (
        "signal_program.py logged the refusal itself; that file is hashed into every "
        "program's artifact_paths, so a future log tweak would invalidate six receipts"
    )


def test_refused_decision_bar_is_logged_by_the_runner_with_the_clock_it_expected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The unsealed half: the runner drains the refusal at the same seam it
    drains a completed stage, and says which clock it wanted."""
    runtime = _runner_runtime()
    program = runtime.program
    assert program is not None
    mis_shaped = _mis_shaped(program.session.timeframe_ms)
    _feed(program, mis_shaped)

    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        runtime.surface_quarantines(_binding())

    records = _quarantine_records(caplog)
    assert len(records) == 1, f"a refused decision bar produced {len(records)} warnings, expected 1"
    record = records[0]
    assert record.action == "bot_decision_bar_quarantined"
    assert record.reason == "TIMEFRAME_MISMATCH"
    assert record.strategy_instance_id == "sid-decision-clock"
    assert record.bar_start_ms == mis_shaped.start_ms
    assert record.bar_end_ms == mis_shaped.end_ms
    assert record.expected_timeframe_ms == program.session.timeframe_ms
    assert record.observed_timeframe_ms == mis_shaped.end_ms - mis_shaped.start_ms
    assert record.quarantined_so_far == 1
    assert program.take_quarantine() is None, "the runner left the refusal undrained"


def test_a_systematically_mis_shaped_feed_is_throttled_but_every_refusal_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A program whose clock never matches its feed refuses every bar. That
    must not become one warning per bar forever, and must not become silence
    either: the count each emitted line carries has to include the refusals
    the throttle held back."""
    runtime = _runner_runtime()
    program = runtime.program
    assert program is not None
    width = program.session.timeframe_ms
    refusals = 3 * _QUARANTINE_LOG_EVERY

    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        for index in range(refusals):
            _feed(program, _mis_shaped(width, index))
            runtime.surface_quarantines(_binding())

    # First occurrence, then one line per _QUARANTINE_LOG_EVERY refusals -- and
    # the last one accounts for all `refusals` of them, so the ones it did not
    # print were counted rather than dropped.
    assert [record.quarantined_so_far for record in _quarantine_records(caplog)] == [
        1,
        _QUARANTINE_LOG_EVERY,
        2 * _QUARANTINE_LOG_EVERY,
        refusals,
    ], f"unexpected throttle pattern over {refusals} identical refusals"


def test_a_refusal_that_is_not_a_clock_mismatch_omits_the_decision_clock_pair(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``UNSETTLED_STAGE`` refuses a bucket of exactly the right width, so
    reporting expected-vs-observed alongside it would point an operator at a
    gate that never fired."""
    runtime = _runner_runtime()
    program = runtime.program
    assert program is not None
    width = program.session.timeframe_ms

    # Stage a well-formed bucket and deliberately never settle it, so the
    # next bucket is refused for a reason that has nothing to do with width.
    _feed(program, bucket(_QUARANTINE_SYMBOL, _BASE_MS, _BASE_MS + width, "100"))
    assert program.session.active_stage is not None, "setup failed: the first bucket did not stage"
    following = bucket(_QUARANTINE_SYMBOL, _BASE_MS + width, _BASE_MS + 2 * width, "100")
    _feed(program, following)

    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        runtime.surface_quarantines(_binding())

    records = _quarantine_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.reason == "UNSETTLED_STAGE"
    assert record.bar_end_ms - record.bar_start_ms == width, (
        "setup failed: the refused bucket was mis-shaped, so this is the width gate after all"
    )
    assert not hasattr(record, "expected_timeframe_ms"), (
        "an UNSETTLED_STAGE refusal reported a decision-clock comparison that did not happen"
    )
    assert not hasattr(record, "observed_timeframe_ms")
