"""Shared harness for the sealed Signal Program test suites.

Everything here is consumed from more than one test *directory* --
``tests/engine/strategy/`` drives the ``SignalProgram``/``SignalSession``
public seam directly, while ``tests/services/test_signal_program_crash_replay.py``
drives the same programs through ``replay_warmup_bars`` -- so none of it can
live in a single directory's ``conftest.py``. The directory-local
construction seam (build a registered program and bind it) lives in
``tests/engine/strategy/conftest.py`` instead, because the crash-replay file
constructs its programs a genuinely different way (through
``_build_signal_strategy``, which yields a live ``_LiveSignalRuntime``).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.base import Strategy, StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration
from app.engine.strategy.signal_intent import SignalIntent

# ---------------------------------------------------------------------------
# Registry-derived program list -- never a hand-written key list.
# ---------------------------------------------------------------------------


def sealed_programs() -> list[tuple[str, StrategyRegistration]]:
    """Every registration that seals a ``signal_program_factory``.

    Derived from ``_STRATEGY_REGISTRY`` rather than hand-listed so that a
    future promotion is covered the moment it is registered, instead of the
    moment somebody remembers to add its key. ``sma_crossover`` was promoted
    with no discard-safety coverage at all (an injected violation left all
    294 of its tests passing) precisely because that list used to be a matter
    of who remembered.
    """
    return [(key, reg) for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]


SEALED_KEYS: tuple[str, ...] = tuple(key for key, _ in sealed_programs())
"""Parametrization source for every per-sealed-program test in this suite."""


class RecordingExecutor:
    """Absorbs every committed ``SignalIntent`` so a COMMIT needs no broker.

    Bound at the strategy's own execution boundary
    (``StrategyContext.set_signal_intent_executor`` -- the same seam
    ``SignalSymbolExecutor``/Backtest bind), which is the only concrete
    execution-facing seam reachable without also standing up the SQLite
    Alpaca Clerk. It therefore doubles as the "broker contact" probe: a test
    can prove both that a commit reached it and that a discarded, Paused, or
    already-captured evaluation never did.
    """

    def __init__(self) -> None:
        self.intents: list[SignalIntent] = []

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


_INITIAL_CASH = Decimal("100000")


def bind_strategy_context(strategy: Strategy) -> tuple[StrategyContext, RecordingExecutor]:
    """Bind a funded context, ``initialize()``, and attach a ``RecordingExecutor``.

    This is the same sequence the live adapter's own
    ``_signal_strategy_evaluations`` performs (bind context, initialize, bind
    an intent executor), except the executor here records instead of the
    adapter's no-op stub.

    Warming a strategy this way -- rather than through ``BacktestEngine`` --
    is deliberate: these strategies declare a fixed backtest window, so
    synthetic bars outside it would be filtered out and the indicators would
    never advance, which is setup that looks like warmup while doing nothing.
    Driving the session directly warms real indicator state and exercises the
    same staged advance/settle path the runner uses.
    """
    context = StrategyContext(portfolio=Portfolio(initial_cash=_INITIAL_CASH))
    strategy.ctx = context
    strategy.initialize()
    executor = RecordingExecutor()
    context.set_signal_intent_executor(executor)
    return context, executor


# ---------------------------------------------------------------------------
# Decision-bucket builders.
# ---------------------------------------------------------------------------

_BUCKET_VOLUME = 1_000


def bucket(symbol: str, start_ms: int, end_ms: int, close: str) -> TradeBar:
    """One decision bucket with explicit ``int64 ms UTC`` bounds."""
    price = Decimal(close)
    return TradeBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=_BUCKET_VOLUME,
    )


def indexed_bucket(symbol: str, index: int, width_ms: int, close: str) -> TradeBar:
    """The ``index``-th epoch-anchored bucket of a run at ``width_ms``.

    Callers pass the *session's own* ``timeframe_ms`` as ``width_ms`` and a
    strictly increasing ``index``, so a bucket built here cannot trip either
    of ``SignalSession.advance``'s quarantine gates (``TIMEFRAME_MISMATCH``,
    ``NON_MONOTONIC_DECISION_CLOCK``). That is what makes a quarantine
    observed while driving these buckets a real signal worth failing on
    rather than skipping past.
    """
    start = index * width_ms
    return bucket(symbol, start, start + width_ms, close)


SEQUENTIAL_BASE_MS = 1_700_000_000_000
"""Arbitrary well-formed epoch anchor for tests that only need internally
consistent, sequential decision buckets -- not calendar-derived, and kept
large enough that a "backwards" offset never goes negative."""


def sequential_bucket(symbol: str, index: int, width_ms: int, close: str) -> TradeBar:
    """The ``index``-th bucket of a run anchored at ``SEQUENTIAL_BASE_MS``.

    ``index`` may be negative, which is how a "backwards" (pre-clock) bucket
    is built without reaching for a negative absolute timestamp.
    """
    start = SEQUENTIAL_BASE_MS + index * width_ms
    return bucket(symbol, start, start + width_ms, close)


# ---------------------------------------------------------------------------
# Custody-surface reflection.
#
# The custody surface is DERIVED, not hand-listed. ``rollback_blocked_entry``
# and ``rollback_blocked_exit`` exist for exactly one purpose -- to undo the
# custody a commit applied -- so the attributes they assign are that
# program's authoritative statement of what "position custody" means for it.
#
# An earlier version of this reflection hard-coded
# ``("_in_position", "_pending_entry", "_open_trade", "_bars_until_exit")``
# and read it with ``hasattr``, so any program whose real attribute names
# differed was silently only partly checked -- the test still reported green.
# ``deployment_validation`` names its own fields ``_entry_pending`` and
# ``_bars_until_exit_signal``; both were invisible to that tuple, and
# injected corruption of either PASSED. Deriving the names from the
# program's own rollback methods makes that class of miss impossible instead
# of fixing the two instances.
# ---------------------------------------------------------------------------

ROLLBACK_METHODS = ("rollback_blocked_entry", "rollback_blocked_exit")

# ...minus whatever the describe phase itself maintains. ``evaluate_signal_bar``
# legitimately tracks *market relation* state on every bar it reads -- SMA's
# ``_prev_short_above_long``, EMA's ``_prev_ema5_above_ema10`` -- and a program
# may also restore that relation on discard, which puts the same name in both
# sets. Holding such a name invariant across a run of discarded bars would be
# asserting that the market never moved, so this test would fail on correct
# code. Custody is the narrower thing only a commit may advance, so the
# describe-maintained names are subtracted out.
#
# This does not silently drop coverage of the subtracted names: EMA's
# ``_bars_until_exit`` preservation is pinned directly by
# ``test_ema_signal_program.py``, and SMA's discarded-EXIT relation restore by
# ``test_signal_program_discard_safety.py::
# test_sma_discarded_exit_stays_reproposable``. Both are *behavioral*
# assertions, which is the right shape for state that is supposed to move.
DESCRIBE_METHOD = "evaluate_signal_bar"


def assigned_self_attrs(strategy: object, method_name: str) -> frozenset[str]:
    """Return the ``self.<attr>`` names one method of ``strategy`` assigns."""
    method = getattr(type(strategy), method_name, None)
    if method is None:
        return frozenset()
    names: set[str] = set()
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                names.add(target.attr)
    return frozenset(names)


def custody_surface(strategy: object) -> frozenset[str]:
    """Names the rollback methods restore that the describe phase does not maintain."""
    restored: set[str] = set()
    for method_name in ROLLBACK_METHODS:
        restored |= assigned_self_attrs(strategy, method_name)
    return frozenset(restored) - assigned_self_attrs(strategy, DESCRIBE_METHOD)


def custody_snapshot(strategy: object, surface: frozenset[str]) -> dict[str, object]:
    """Freeze the current value of every name on a program's custody surface."""
    # getattr without a default on purpose: every derived name must really
    # exist. A rollback method assigning an attribute the instance does not
    # have is itself a defect, and silently skipping it is how an earlier
    # version of this reflection lost coverage.
    return {name: repr(getattr(strategy, name)) for name in sorted(surface)}
