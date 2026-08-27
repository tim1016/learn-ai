"""Directory-local construction seam for the sealed Signal Program tests.

Every module in this package builds a registered program the same way --
construct it from the registry, bind a funded ``StrategyContext``,
``initialize()``, bind a recording intent executor -- before driving it at
whichever seam that module is about (``session.advance`` directly, or the
production ``capture_source_bar``/``on_consolidated_bar`` entrypoint). That
sequence lives here so every module in this package shares one copy of it.

Split of responsibility with ``tests/_helpers/signal_program.py``: the pure,
directory-crossing pieces (registry-derived ``SEALED_KEYS``,
``RecordingExecutor``, decision-bucket builders, custody introspection) live
there because ``tests/services/test_signal_program_crash_replay.py`` drives
the same sealed programs through ``replay_warmup_bars`` and needs them too.
What stays here is what only this directory uses -- ``build_program`` is not
shared with the crash-replay file, which constructs its programs through
``_build_signal_strategy`` into a live ``_LiveSignalRuntime`` instead.

These are plain module-level definitions rather than fixtures on purpose:
a ``@pytest.mark.parametrize`` source and a module-level driving helper both
run before any fixture can be injected, and every consumer in this package
is one or the other.
"""

from __future__ import annotations

from typing import NamedTuple

from app.engine.strategy.base import StrategyContext
from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import SignalProgram
from tests._helpers.signal_program import RecordingExecutor, bind_strategy_context


class PreparedProgram(NamedTuple):
    """A constructed program, the params it was built from, its bound context, and its executor."""

    program: SignalProgram
    params: StrategyParamsBase
    executor: RecordingExecutor
    context: StrategyContext


def build_program(key: str) -> PreparedProgram:
    """Construct the registered program for ``key`` and bind it, ready to drive.

    Left un-activated: a caller that drives ``session.advance`` directly
    supplies the evaluation mode per call and needs no activation, while a
    caller exercising ``on_consolidated_bar`` chooses ``activate_for_runner``
    (mode captured per bar) or ``activate_for_backtest`` (DECIDE default)
    itself, because which one is in force is exactly the axis those tests
    vary.
    """
    registration = _STRATEGY_REGISTRY[key]
    assert registration.signal_program_factory is not None, f"'{key}' has no signal_program_factory"
    params = registration.param_schema()
    program = registration.signal_program_factory(params)
    context, executor = bind_strategy_context(program.strategy)
    return PreparedProgram(program=program, params=params, executor=executor, context=context)
