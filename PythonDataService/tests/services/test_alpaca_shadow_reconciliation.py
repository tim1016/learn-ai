"""Shadow-vs-paper-twin reconciliation gates on decisions and reports prices (ADR 0059 D2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.models import RunResource
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.signal_program_seal import SealedBotProgram
from app.services.alpaca_shadow_reconciliation import (
    FILL_PRICE_ATOL,
    ShadowTwinMismatch,
    TwinFill,
    evaluate_shadow_gate,
    reconcile_twin_day,
    twins_agree,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.session_authority import et_minute_of_day_ms

DAY = date(2026, 9, 8)
OPEN = session_open_ms_utc(DAY)
CLOSE = session_close_ms_utc(DAY)
SID, TWIN = "ema-shadow-1", "ema-paper-1"


def _fill(
    minute: int, *, side: str = "buy", qty: str = "1", price: str = "100.00", ref: str = "a"
) -> TwinFill:
    return TwinFill(
        symbol="SPY",
        side=side,
        quantity=Decimal(qty),
        fill_price=Decimal(price),
        filled_at_ms=et_minute_of_day_ms(DAY, minute),
        order_ref=f"learn-ai/x/v1:{ref}",
    )


def _reconcile(shadow: list[TwinFill], twin: list[TwinFill]):
    return reconcile_twin_day(
        session_open_ms=OPEN,
        strategy_instance_id=SID,
        twin_strategy_instance_id=TWIN,
        shadow_fills=tuple(shadow),
        twin_fills=tuple(twin),
    )


def test_identical_days_pass_with_no_divergence_and_a_stable_digest() -> None:
    first = _reconcile(
        [_fill(600), _fill(660, side="sell")],
        [_fill(600, ref="p"), _fill(660, side="sell", ref="q")],
    )
    second = _reconcile(
        [_fill(600), _fill(660, side="sell")],
        [_fill(600, ref="p"), _fill(660, side="sell", ref="q")],
    )
    assert first.passed is True and first.divergences == ()
    assert first.report_sha256() == second.report_sha256()
    assert len(first.report_sha256()) == 64


@pytest.mark.parametrize(
    ("shadow", "twin", "category"),
    [
        ([_fill(600)], [], DivergenceCategory.DECISION_MISMATCH),
        ([_fill(600)], [_fill(600, side="sell")], DivergenceCategory.DIRECTION_MISMATCH),
        ([_fill(600, qty="2")], [_fill(600, qty="1")], DivergenceCategory.QUANTITY_MISMATCH),
    ],
)
def test_shape_divergences_gate(
    shadow: list[TwinFill], twin: list[TwinFill], category: DivergenceCategory
) -> None:
    result = _reconcile(shadow, twin)
    assert [d.category for d in result.gating] == [category]
    assert result.passed is False


def test_price_drift_is_reported_never_gated() -> None:
    result = _reconcile([_fill(600, price="100.00")], [_fill(600, price="100.07")])
    assert [d.category for d in result.divergences] == [DivergenceCategory.FILL_PRICE_DRIFT]
    assert result.gating == () and result.passed is True
    assert result.max_fill_price_drift == Decimal("0.07")
    assert result.fill_price_atol == FILL_PRICE_ATOL == Decimal("0.01")
    within = _reconcile([_fill(600, price="100.00")], [_fill(600, price="100.01")])
    assert within.divergences == () and within.max_fill_price_drift == Decimal("0.01")


def _seal(**overrides: object) -> SealedBotProgram:
    base = dict(
        configured_signal_hash="a" * 64,
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        carryover_policy="FORBID",
        mode="trade",
        sealed_account_id="PA-TEST",
    )
    return SealedBotProgram.model_construct(**{**base, **overrides})


def test_twins_agree_on_the_configured_signal_and_plan_only() -> None:
    shadow = _seal(sealed_account_id="shadow:9LIVE0001")
    assert twins_agree(shadow, _seal()) is None
    assert twins_agree(shadow, _seal(configured_signal_hash="b" * 64)) is not None
    assert twins_agree(shadow, _seal(quantity=2)) is not None
    assert twins_agree(shadow, _seal(mode="dry_run")) is not None
    assert twins_agree(_seal(), _seal()) is not None  # the shadow side must be shadow-sealed


class _Source:
    def __init__(self, fills: dict[str, list[TwinFill]], runs: dict[str, list[RunResource]]) -> None:
        self._fills, self._runs = fills, runs

    def fills_between(
        self, *, strategy_instance_id: str, from_ms: int, to_ms: int
    ) -> tuple[TwinFill, ...]:
        return tuple(
            f
            for f in self._fills.get(strategy_instance_id, ())
            if from_ms <= f.filled_at_ms <= to_ms
        )

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        return tuple(self._runs.get(strategy_instance_id, ()))


def _run(started_ms: int, stopped_ms: int | None) -> RunResource:
    return RunResource(
        run_id="run-1",
        strategy_instance_id=SID,
        lifecycle_run_id="l-1",
        state="ACTIVE" if stopped_ms is None else "STOPPED",
        started_at_ms=started_ms,
        stopped_at_ms=stopped_ms,
    )


def _binding(sid: str, sealed: str) -> BrokerBotBinding:
    # ``model_construct`` on both halves: the validating constructor re-runs
    # ``SealedBotProgram``'s nested-hash validator, which needs a whole
    # configured-signal seal this gate never reads.
    return BrokerBotBinding.model_construct(
        strategy_instance_id=sid,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id=sealed,
        run_id="run-1",
        created_at_ms=0,
        sealed_program=_seal(sealed_account_id=sealed),
    )


def _clean_day(ledger: ShadowSessionLedger, *, opened_minute: int = 180) -> None:
    ledger.append(
        kind="day_opened",
        session_open_ms=OPEN,
        observed_at_ms=et_minute_of_day_ms(DAY, opened_minute),
        verdict="clean",
    )
    ledger.append(
        kind="session_closed_clean",
        session_open_ms=OPEN,
        observed_at_ms=et_minute_of_day_ms(DAY, 1200),
        verdict="clean",
    )


def _evaluate(tmp_path: Path, *, ledger: ShadowSessionLedger, source: _Source, required: int = 1):
    return evaluate_shadow_gate(
        live_account_id="9LIVE0001",
        shadow_binding=_binding(SID, "shadow:9LIVE0001"),
        twin_binding=_binding(TWIN, "PA-TEST"),
        twin_account_id="PA-TEST",
        required_sessions=required,
        session_ledger=ledger,
        shadow_source=source,
        twin_source=source,
        window=ALPACA_EXTENDED_HOURS_WINDOW,
        now_ms=et_minute_of_day_ms(date(2026, 9, 9), 60),
    )


def test_a_clean_covered_reconciled_day_counts(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger)
    source = _Source({SID: [_fill(600)], TWIN: [_fill(600, ref="p")]}, {SID: [_run(OPEN - 1, None)]})

    evaluation = _evaluate(tmp_path, ledger=ledger, source=source)

    [verdict] = evaluation.sessions
    assert (verdict.state, verdict.shadow_run_id) == ("counted", "run-1")
    assert verdict.reconciliation is not None and verdict.reconciliation.passed
    assert evaluation.satisfied is True and evaluation.counted == (verdict,)


@pytest.mark.parametrize(
    ("opened_minute", "run", "twin_fills", "state"),
    [
        (600, _run(OPEN - 1, None), [_fill(600, ref="p")], "sweep_opened_late"),
        (180, _run(OPEN + 1, None), [_fill(600, ref="p")], "run_not_covering"),
        (180, _run(OPEN - 1, CLOSE - 1), [_fill(600, ref="p")], "run_not_covering"),
        (180, _run(OPEN - 1, None), [], "twin_diverged"),
    ],
)
def test_days_that_do_not_count_say_why(
    tmp_path: Path,
    opened_minute: int,
    run: RunResource,
    twin_fills: list[TwinFill],
    state: str,
) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger, opened_minute=opened_minute)
    source = _Source({SID: [_fill(600)], TWIN: twin_fills}, {SID: [run]})

    evaluation = _evaluate(tmp_path, ledger=ledger, source=source)

    assert [v.state for v in evaluation.sessions] == [state]
    assert evaluation.satisfied is False


def test_a_non_clean_day_and_a_seal_mismatch_are_named(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    ledger.append(
        kind="day_opened",
        session_open_ms=OPEN,
        observed_at_ms=et_minute_of_day_ms(DAY, 180),
        verdict="stale",
    )
    ledger.append(
        kind="non_clean",
        session_open_ms=OPEN,
        observed_at_ms=et_minute_of_day_ms(DAY, 180),
        verdict="stale",
    )
    source = _Source({}, {SID: [_run(OPEN - 1, None)]})
    assert [v.state for v in _evaluate(tmp_path, ledger=ledger, source=source).sessions] == [
        "sweep_not_clean"
    ]

    with pytest.raises(ShadowTwinMismatch, match="configured signal"):
        evaluate_shadow_gate(
            live_account_id="9LIVE0001",
            shadow_binding=_binding(SID, "shadow:9LIVE0001"),
            twin_binding=_binding(TWIN, "PA-TEST").model_copy(
                update={"sealed_program": _seal(configured_signal_hash="b" * 64)}
            ),
            twin_account_id="PA-TEST",
            required_sessions=1,
            session_ledger=ledger,
            shadow_source=source,
            twin_source=source,
            window=ALPACA_EXTENDED_HOURS_WINDOW,
            now_ms=CLOSE + 1,
        )
