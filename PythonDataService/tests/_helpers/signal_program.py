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
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.base import Strategy, StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration
from app.engine.strategy.signal_intent import SignalIntent
from app.engine.strategy.signal_program import EvaluationMode, EvaluationTrace
from app.lean_sidecar import trading_calendar

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


def mark_bar_arrival(context: StrategyContext, bar: TradeBar) -> None:
    """Stamp the decision clock and refresh the reference price, as both
    production drivers do before a bucket's handler runs.

    ``StrategyContext.register_consolidator``'s ``_on_emit`` wrapper
    (``app/engine/strategy/base.py``) sets exactly these two fields on every
    fired bucket, and ``bot_trade_strategy._drain_bar`` sets
    ``current_time_ms`` again per minute bar. A test that drives
    ``SignalSession.advance`` directly bypasses both, so it calls this
    instead, before every ``advance``.

    Not cosmetic: a program whose ``commit_signal_decision`` reaches the
    portfolio directly rather than through ``ctx.emit_signal_intent``
    (``instrument_surface="explicit"`` -- ``deployment_validation`` calls
    ``ctx.set_holdings``/``ctx.liquidate``) asserts on ``current_time_ms``
    and raises without a reference price, so a COMMIT would crash instead
    of committing -- or, in a live-loop continuation, stamp every order
    with one stale clock and look like duplicate broker contact.
    """
    context.current_time_ms = bar.end_ms
    context.portfolio.update_reference_price(bar.symbol, bar.close)


# ---------------------------------------------------------------------------
# Decision-bucket builders.
# ---------------------------------------------------------------------------

_BUCKET_VOLUME = 1_000
_BUCKET_WICK = Decimal("1")

BarBody = Literal["flat", "green", "red"]
"""Which way a synthetic bucket's body points -- ``close`` vs ``open``.

Not decoration: ``deployment_validation`` is the one sealed program whose
*entry condition is the bar body itself* ("two consecutive green minute
bars", ``close > open``). Every bucket this module built used to carry
``open == close``, so that program could never propose a single ENTER
anywhere in these suites -- the parametrized assertions would have passed
while proving nothing about it.
"""

_BODY_OPEN_OFFSET: dict[BarBody, Decimal] = {
    "flat": Decimal("0"),
    "green": -_BUCKET_WICK,
    "red": _BUCKET_WICK,
}


def bucket(symbol: str, start_ms: int, end_ms: int, close: str, *, body: BarBody = "flat") -> TradeBar:
    """One decision bucket with explicit ``int64 ms UTC`` bounds.

    ``body`` moves only ``open``, onto the bucket's own low (``green``) or
    high (``red``); ``high``/``low``/``close`` are identical for all three
    values. That is deliberate: every indicator any other sealed program
    reads is fed from ``high``/``low``/``close``, so a caller may make a
    bucket green or red for ``deployment_validation`` without perturbing a
    single indicator input anywhere else. ``flat`` (the default) is the
    original ``open == close`` shape, byte-for-byte.
    """
    price = Decimal(close)
    return TradeBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        open=price + _BODY_OPEN_OFFSET[body],
        high=price + _BUCKET_WICK,
        low=price - _BUCKET_WICK,
        close=price,
        volume=_BUCKET_VOLUME,
    )


# ---------------------------------------------------------------------------
# Calendar-derived anchors.
#
# Synthetic buckets used to be epoch-anchored (``index * width_ms``, i.e.
# 1970-01-01) or pinned to a bare epoch literal that happened to land on a
# real session. Neither survives a calendar-aware program:
# ``deployment_validation`` asks the canonical NYSE calendar which session a
# bar belongs to, and an epoch-anchored bucket kills it outright with
# ``LookupError: 1970-01-01 is not a NYSE session``. Both anchors below are
# therefore derived from ``app.lean_sidecar.trading_calendar`` -- the repo's
# sole ``mcal.get_calendar`` caller -- never from a hardcoded session
# literal or a fixed ET offset, per ``.claude/rules/temporal-rigor.md``.
# ---------------------------------------------------------------------------

_ANCHOR_SEARCH_START = date(2024, 1, 2)
_ANCHOR_SEARCH_HORIZON_DAYS = 30


@lru_cache(maxsize=1)
def anchor_session_date() -> date:
    """The real, ordinary (non-early-close) NYSE session both anchors sit in.

    Ordinary rather than merely "a trading day": an early close would put a
    half-day's shortened stop/flatten barrier in the middle of a long
    synthetic run, which is a boundary case
    ``test_signal_program_session_boundaries.py`` covers deliberately and
    every other suite would only trip over by accident.
    """
    horizon = _ANCHOR_SEARCH_START + timedelta(days=_ANCHOR_SEARCH_HORIZON_DAYS)
    for window in trading_calendar.session_windows_ms_utc(_ANCHOR_SEARCH_START, horizon):
        if not trading_calendar.is_early_close(window.session_date):
            return window.session_date
    raise AssertionError(
        f"no ordinary NYSE session found within {_ANCHOR_SEARCH_HORIZON_DAYS} days of {_ANCHOR_SEARCH_START}"
    )


@lru_cache(maxsize=1)
def session_open_anchor_ms() -> int:
    """``int64 ms UTC`` of :func:`anchor_session_date`'s scheduled open."""
    return trading_calendar.session_open_ms_utc(anchor_session_date())


@lru_cache(maxsize=1)
def session_midpoint_anchor_ms() -> int:
    """``int64 ms UTC`` halfway through :func:`anchor_session_date`'s session.

    Distinct from :func:`session_open_anchor_ms` for one concrete reason:
    ``sequential_bucket`` below is the builder that must accept a NEGATIVE
    index (that is how a "backwards" pre-clock bucket is built), and only a
    mid-session anchor can promise that index ``-1`` still lands inside the
    same real session rather than before its open.
    """
    session_date = anchor_session_date()
    open_ms = trading_calendar.session_open_ms_utc(session_date)
    close_ms = trading_calendar.session_close_ms_utc(session_date)
    return open_ms + (close_ms - open_ms) // 2


def indexed_bucket(
    symbol: str, index: int, width_ms: int, close: str, *, body: BarBody = "flat"
) -> TradeBar:
    """The ``index``-th bucket of a run starting at the anchor session's open.

    Callers pass the *session's own* ``timeframe_ms`` as ``width_ms`` and a
    strictly increasing ``index``, so a bucket built here cannot trip either
    of ``SignalSession.advance``'s quarantine gates (``TIMEFRAME_MISMATCH``,
    ``NON_MONOTONIC_DECISION_CLOCK``). That is what makes a quarantine
    observed while driving these buckets a real signal worth failing on
    rather than skipping past.

    Anchored at the real scheduled open (not epoch 0) so ``index`` is also a
    meaningful *offset into a session* for a calendar-aware program: index 0
    is that session's first bucket.
    """
    start = session_open_anchor_ms() + index * width_ms
    return bucket(symbol, start, start + width_ms, close, body=body)


def sequential_bucket(
    symbol: str, index: int, width_ms: int, close: str, *, body: BarBody = "flat"
) -> TradeBar:
    """The ``index``-th bucket of a run anchored mid-session.

    ``index`` may be negative, which is how a "backwards" (pre-clock) bucket
    is built without reaching for a negative absolute timestamp -- and, with
    the mid-session anchor, without leaving the anchor session either.
    """
    start = session_midpoint_anchor_ms() + index * width_ms
    return bucket(symbol, start, start + width_ms, close, body=body)


# ---------------------------------------------------------------------------
# Custody-surface reflection.
#
# The custody surface is DERIVED, not hand-listed. ``rollback_blocked_entry``
# and ``rollback_blocked_exit`` name exactly the state a commit advances, so
# the attributes they assign are that program's authoritative statement of what
# "position custody" means for it.
#
# READ THIS BEFORE DELETING THEM AS DEAD CODE. Nothing at runtime calls these
# two methods any more. Under the staged protocol (#1730) a strategy mutates
# position custody only in ``commit_signal_decision``, so a discarded candidate
# has nothing to undo and ``bot_trade_strategy._discard_evaluation`` simply
# settles DISCARD; the compensating-rollback branches that used to call them
# were removed. Their remaining job is to be READ, not run: this module derives
# each program's custody surface from what they assign, and the
# session-boundary, discard-safety and pause invariants assert against that
# derived surface. Both consumers already refuse an empty surface explicitly
# (``assert surface, ...``), so deleting these methods fails loudly rather than
# passing vacuously -- but it fails saying "this program declares no custody
# surface", which reads like a problem with the program rather than with the
# removal. This paragraph is here so the next reader recognizes which it is.
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


def placeholder_evaluation_trace(
    *,
    evaluation_id: str,
    bar_close_ms: int,
    program_key: str = "ema_crossover_signal",
    program_version: str = "v1",
    staged_candidate: str | None = None,
) -> EvaluationTrace:
    """A structurally valid trace for a test that needs one but asserts nothing on it.

    ``StrategyEvaluation.trace`` is non-optional (issue #1736), so a test
    constructing an evaluation by hand has to supply a trace even when the
    behaviour under test is about settlement or receipts rather than decision
    content. Shared rather than re-inlined per file so the shape follows
    ``EvaluationTrace`` when a field is added, instead of N copies drifting.

    Not for tests that assert on trace *content* -- those should build the
    trace they mean, so the values under assertion are visible in the test.
    """
    return EvaluationTrace(
        program_key=program_key,
        program_version=program_version,
        evaluation_id=evaluation_id,
        bar_close_ms=bar_close_ms,
        bar_qualified=True,
        bucket_closed=True,
        ready=True,
        relation_facts={},
        signal_facts={},
        staged_candidate=staged_candidate,
        reason_evidence={},
        action_plan_request=None,
        evaluation_mode=EvaluationMode.DECIDE,
    )
