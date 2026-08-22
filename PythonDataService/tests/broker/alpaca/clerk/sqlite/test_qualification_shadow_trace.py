"""Tests for shadow-mode canonical trace parity (issue #1729 AC #2, AC #11).

AC #2: "Shadow mode compares canonical traces without broker contact and
fails closed on the first divergent authority edge." AC #11: tests here use
only controlled ports (`_QualifiedBarFeed`, a pinned CSV fixture) and never
place an external Alpaca paper order.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk import (
    get_alpaca_clerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    get_clerk_runtime,
    register_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.qualification_shadow_trace import (
    ShadowTraceDivergence,
    ShadowTraceDivergenceError,
    UnsupportedShadowProgramError,
    compare_canonical_traces,
    run_shadow_trace_evaluation,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.signal_program import EvaluationTrace

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "fixtures/golden/cross-engine-studies/cells/SPY_W3mo_2026-02-02_to_2026-04-30/lean/observations.csv"
)
# Same truncation point `tests/services/test_bot_runner.py::_ema_parity_bars_through_first_exit`
# already uses -- the pinned window covering exactly one ENTER/EXIT round trip.
_EMA_FIRST_EXIT_MS = 1_770_393_600_000


def _load_bars_through_first_round_trip(symbol: str = "SPY") -> list[TradeBar]:
    """Load a pinned, qualified bar window covering one full ENTER/EXIT cycle."""
    bars: list[TradeBar] = []
    with _FIXTURE.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                TradeBar(
                    symbol=symbol,
                    start_ms=end_ms - 60_000,
                    end_ms=end_ms,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(Decimal(row["volume"])),
                )
            )
            if end_ms > _EMA_FIRST_EXIT_MS:
                break
    return bars


def _sample_trace(**overrides: Any) -> EvaluationTrace:
    base: dict[str, Any] = {
        "program_key": "p",
        "program_version": "v1",
        "evaluation_id": "e1",
        "bar_close_ms": 1,
        "bar_qualified": True,
        "bucket_closed": True,
        "ready": True,
        "relation_facts": {},
        "signal_facts": {},
        "staged_candidate": None,
        "reason_evidence": {},
        "action_plan_request": None,
    }
    base.update(overrides)
    return EvaluationTrace(**base)


class _DetonatingClerk:
    """An ``ActiveAlpacaClerk`` double whose broker-facing methods must never fire.

    Static identity attributes (``authority_kind``, ``account_id``,
    ``broker_id``) let it install cleanly through the real ``set_alpaca_clerk``
    / ``register_clerk_runtime`` production accessors -- the exact
    authority-selection seam ``run_trade_bot`` / ``run_dry_run_bot`` use.
    Every method that would constitute Clerk-or-broker contact raises and
    counts the attempt, so a future accidental wiring of shadow evaluation
    through either accessor fails this test immediately.
    """

    broker_id = "detonator"

    def __init__(self, *, authority_kind: str, account_id: str) -> None:
        self.authority_kind = authority_kind
        self.account_id = account_id
        self.contact_count = 0

    def _detonate(self, method: str) -> None:
        self.contact_count += 1
        raise AssertionError(f"shadow evaluation must never call Clerk.{method}")

    async def recover(self) -> None:
        self._detonate("recover")

    async def unresolved_effect_count(self) -> int:
        self._detonate("unresolved_effect_count")
        return 0

    async def register_strategy_run(self, binding: Any, *, admission_snapshot: Any = None) -> None:
        del binding, admission_snapshot
        self._detonate("register_strategy_run")

    async def stop_strategy_run(
        self, *, strategy_instance_id: str, run_id: str, reason: str | None = None
    ) -> None:
        del strategy_instance_id, run_id, reason
        self._detonate("stop_strategy_run")

    async def execute_for_instance(self, **kwargs: Any) -> None:
        del kwargs
        self._detonate("execute_for_instance")


@pytest.mark.asyncio
async def test_shadow_trace_reproduces_the_first_ema_round_trip() -> None:
    """The live-adapter trace matches the Backtest-canonical trace exactly."""
    bars = _load_bars_through_first_round_trip()

    result = await run_shadow_trace_evaluation("ema_crossover_signal", "SPY", None, bars)

    assert result.compared_count > 0
    candidates = [trace.staged_candidate for trace in result.traces]
    assert candidates.count("ENTER") == 1
    assert candidates.count("EXIT") == 1


@pytest.mark.asyncio
async def test_shadow_trace_evaluation_rejects_a_compatibility_mode_strategy() -> None:
    """`deployment_validation` has no SignalSession, so it has no trace to shadow."""
    with pytest.raises(UnsupportedShadowProgramError):
        await run_shadow_trace_evaluation("deployment_validation", "SPY", None, [])


def test_compare_canonical_traces_accepts_identical_sequences() -> None:
    compare_canonical_traces([_sample_trace()], [_sample_trace()])


def test_compare_canonical_traces_fails_closed_on_the_first_divergent_field() -> None:
    expected = [_sample_trace(), _sample_trace(evaluation_id="e2", ready=False)]
    observed = [_sample_trace(), _sample_trace(evaluation_id="e2", ready=True)]

    with pytest.raises(ShadowTraceDivergenceError) as excinfo:
        compare_canonical_traces(expected, observed)

    assert excinfo.value.divergence == ShadowTraceDivergence(
        index=1, evaluation_id="e2", field="ready", expected=False, observed=True
    )


def test_compare_canonical_traces_stops_at_the_first_index_not_a_later_one() -> None:
    """Both index 0 and index 1 diverge; only index 0 may ever be reported --
    proves the comparator stops immediately instead of accumulating a diff."""
    expected = [_sample_trace(ready=False), _sample_trace(evaluation_id="e2")]
    observed = [_sample_trace(ready=True), _sample_trace(evaluation_id="e2-different")]

    with pytest.raises(ShadowTraceDivergenceError) as excinfo:
        compare_canonical_traces(expected, observed)

    assert excinfo.value.divergence.index == 0
    assert excinfo.value.divergence.field == "ready"


def test_compare_canonical_traces_fails_closed_on_a_length_mismatch() -> None:
    expected = [_sample_trace()]
    observed = [_sample_trace(), _sample_trace(evaluation_id="e2")]

    with pytest.raises(ShadowTraceDivergenceError) as excinfo:
        compare_canonical_traces(expected, observed)

    assert excinfo.value.divergence.index == 1
    assert excinfo.value.divergence.field == "trace_presence"


@pytest.mark.asyncio
async def test_shadow_trace_evaluation_never_contacts_the_active_clerk_or_broker() -> None:
    """AC #11 / BUILD item 3: install a real-authority AND a synthetic-authority
    Clerk double whose every method raises if invoked, run the full shadow
    evaluation over a bar window that produces a real ENTER and EXIT, and
    prove neither double was ever touched. This would FAIL -- not merely
    warn -- if shadow evaluation's driver ever routed a decision through
    `get_alpaca_clerk()` / `get_clerk_runtime()`.
    """
    real_authority = _DetonatingClerk(authority_kind="sqlite", account_id="detonator-real")
    synthetic_account_id = "sim:shadow-trace-ema_crossover_signal-SPY"
    synthetic_authority = _DetonatingClerk(
        authority_kind="synthetic", account_id=synthetic_account_id
    )
    set_alpaca_clerk(real_authority)
    register_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="synthetic",
            clerk=synthetic_authority,
            account_id=synthetic_account_id,
            account_authority_kind="synthetic",
        )
    )
    try:
        # Sanity: both doubles really are reachable through the production
        # accessors -- otherwise a zero contact_count would prove nothing.
        assert get_alpaca_clerk() is real_authority
        assert get_clerk_runtime(synthetic_account_id) is not None

        bars = _load_bars_through_first_round_trip()
        result = await run_shadow_trace_evaluation("ema_crossover_signal", "SPY", None, bars)

        assert result.compared_count > 0
        assert "ENTER" in [trace.staged_candidate for trace in result.traces]
        assert real_authority.contact_count == 0
        assert synthetic_authority.contact_count == 0
    finally:
        reset_alpaca_clerk_for_testing()
