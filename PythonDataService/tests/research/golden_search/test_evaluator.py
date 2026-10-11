"""The study evaluator's pure contract: cache identity, capability, the common run-up and the engine projection (#2696)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.golden_search.declarations import point_hash as hash_point
from app.research.golden_search.evaluator import (
    EXIT_AT_WINDOW_END,
    CapabilityError,
    DeliveredOutcome,
    EvaluationCapability,
    EvaluationRequest,
    EvaluationResult,
    EvidenceIntegrityError,
    StudyEvaluator,
    context_digest,
    detail_integrity_problem,
    engine_request,
    evaluation_key,
    execution_context,
    project_response,
    reordered_dispatch,
    serial_dispatch,
    warmup_session,
)
from app.research.golden_search.protocol import BASE_SCENARIO, ExecutionAssumptions, StressScenario
from app.research.golden_search.selection import Metrics
from app.schemas.engine_backtest import EngineBacktestResponse, EngineTradeResponse
from app.utils.session_anchors import et_midnight_ms

DAY = 24 * 60 * 60 * 1000
JAN = (et_midnight_ms(date(2025, 1, 6)), et_midnight_ms(date(2025, 1, 8)))
POINT = {"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


def _context(**overrides: object) -> dict:
    identity = {"git_revision": "abc", "tree_state": "clean", "source_digest": "s" * 64, "environment_digest": "e" * 64, "digest_scheme": 2}
    base = execution_context(
        strategy_key="ema_crossover_signal",
        program_version="ema-crossover-signal/v1",
        parameter_schema_version="ema-crossover-signal-params/v3",
        code_identity=identity,
        data_snapshot_digest="d" * 64,
        execution=ExecutionAssumptions(),
        data_policy={"source": "polygon", "adjusted": True, "session": "regular"},
        run_up={"required_samples": 16, "bar_span_ms": 900_000, "run_up_sessions": 1},
    )
    return {**base, **overrides}


def test_evaluation_key_changes_with_every_part_of_its_identity() -> None:
    digest = context_digest(_context())
    key = evaluation_key(digest, "p" * 64, JAN, "base", False)

    assert key == evaluation_key(digest, "p" * 64, JAN, "base", False)
    variants = {
        evaluation_key(context_digest(_context(data_snapshot_digest="x" * 64)), "p" * 64, JAN, "base", False),
        evaluation_key(digest, "q" * 64, JAN, "base", False),
        evaluation_key(digest, "p" * 64, (JAN[0], JAN[1] + DAY), "base", False),
        evaluation_key(digest, "p" * 64, JAN, "slippage_1c", False),
        evaluation_key(digest, "p" * 64, JAN, "base", True),
    }
    assert key not in variants and len(variants) == 5


def test_the_context_digest_ignores_provenance_but_not_the_code_it_runs() -> None:
    base = _context()
    moved_commit = {**base, "code_identity": {**base["code_identity"], "git_revision": "def", "tree_state": "dirty"}}
    changed_code = {**base, "code_identity": {**base["code_identity"], "source_digest": "t" * 64}}

    assert context_digest(moved_commit) == context_digest(base)
    assert context_digest(changed_code) != context_digest(base)


def test_the_capability_admits_a_window_inside_an_interval_and_refuses_one_that_reaches_past_it() -> None:
    development = (et_midnight_ms(date(2025, 1, 1)), et_midnight_ms(date(2025, 4, 1)))
    capability = EvaluationCapability(allowed=(development,))

    capability.check(development)
    capability.check((development[0] + DAY, development[1] - DAY))
    for window in ((development[0], development[1] + DAY), (development[1], development[1] + 30 * DAY), (development[0], development[0])):
        with pytest.raises(CapabilityError):
            capability.check(window)


def test_the_warmup_session_counts_trading_sessions_back_from_the_window() -> None:
    # 2025-01-01 is a holiday: the sessions before it are Dec 31 and Dec 30.
    assert warmup_session(et_midnight_ms(date(2025, 1, 1)), 2) == date(2024, 12, 30)
    # Christmas is skipped; the half-day before it counts as a session.
    assert warmup_session(et_midnight_ms(date(2024, 12, 26)), 1) == date(2024, 12, 24)
    with pytest.raises(ValueError):
        warmup_session(JAN[0], 0)


def _request(**overrides: object) -> EvaluationRequest:
    base = dict(
        point=POINT,
        point_hash="p" * 64,
        window=JAN,
        warmup_from=date(2025, 1, 3),
        scenario=None,
        detail=False,
        stage="search",
        fold_index=None,
    )
    return EvaluationRequest(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_engine_request_scores_the_window_after_the_common_run_up() -> None:
    execution = ExecutionAssumptions(fill_mode="next_bar_open", commission_per_order=1.0, slippage_per_share=0.01, initial_cash=50_000.0)

    request = engine_request(_request(), strategy_key="ema_crossover_signal", execution=execution)

    assert (request.from_date, request.to_date, request.warmup_from_date) == ("2025-01-06", "2025-01-07", "2025-01-03")
    assert request.params == POINT and request.strategy_name == "ema_crossover_signal"
    assert (request.fill_mode, request.commission_per_order, request.slippage_per_share, request.initial_cash) == (
        "next_bar_open",
        1.0,
        0.01,
        50_000.0,
    )
    assert request.summary_only and not request.save_study and not request.auto_fetch and request.resolution == "minute"


def test_a_stress_scenario_adds_its_costs_and_a_detail_run_keeps_the_per_bar_artifacts() -> None:
    execution = ExecutionAssumptions(commission_per_order=1.0, slippage_per_share=0.01)
    scenario = StressScenario("harsh", "Harsh", slippage_add=0.02, commission_add=0.5, fill_mode="signal_bar_close")

    request = engine_request(_request(scenario=scenario, detail=True), strategy_key="ema_crossover_signal", execution=execution)

    assert request.commission_per_order == pytest.approx(1.5, abs=1e-12)
    assert request.slippage_per_share == pytest.approx(0.03, abs=1e-12)
    assert request.fill_mode == "signal_bar_close" and not request.summary_only


def _response(**overrides: object) -> EngineBacktestResponse:
    days = (date(2025, 1, 6), date(2025, 1, 7))
    curve = []
    for index, day in enumerate(days):
        open_ms = session_open_ms_utc(day)
        curve.append({"timestamp": open_ms + 60_000, "equity": 100_000.0 + index * 10, "cash": 0.0, "holdings_value": 0.0})
        curve.append({"timestamp": session_close_ms_utc(day), "equity": 100_500.0 + index * 250, "cash": 0.0, "holdings_value": 0.0})
    trades = [
        EngineTradeResponse(trade_number=1, entry_time=curve[0]["timestamp"], entry_price=500.0, exit_time=curve[1]["timestamp"], exit_price=502.5,
                            quantity=200, indicators={"rsi": 55.0}, pnl_pts=2.5, pnl_pct=0.005, result="WIN"),
        EngineTradeResponse(trade_number=2, entry_time=curve[2]["timestamp"], entry_price=502.0, exit_time=curve[3]["timestamp"], exit_price=501.0,
                            quantity=100, pnl_pts=-1.0, pnl_pct=-0.002, result="LOSS", is_synthetic_exit=True),
    ]
    base = dict(
        success=True, strategy_name="ema_crossover_signal", fill_mode="decision_minute_open", initial_cash=100_000.0,
        final_equity=100_750.0, net_profit=750.0, total_fees=0.0, total_trades=2, winning_trades=1, losing_trades=1, win_rate=0.5,
        statistics={"net_profit": 750.0, "net_profit_pct": 0.0075, "sharpe_ratio": 1.25, "max_drawdown_pct": 0.004, "win_rate": 0.5},
        trades=trades, equity_curve=curve,
        # The engine keeps each ET date's last sample on every run, a summary run included.
        equity_day_ends=[{"timestamp": point["timestamp"], "equity": point["equity"]} for point in curve[1::2]],
    )
    return EngineBacktestResponse(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_projection_keeps_the_sweep_statistics_and_bounded_daily_detail() -> None:
    result = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0)

    metrics = result.metrics
    assert (metrics.status, metrics.total_trades, metrics.net_profit, metrics.total_return_pct) == ("completed", 2, 750.0, 0.0075)
    assert (metrics.sharpe_ratio, metrics.max_drawdown_pct, metrics.win_rate) == (1.25, 0.004, 0.5)
    assert result.detail is not None
    assert result.detail["daily_equity"] == [
        [session_close_ms_utc(date(2025, 1, 6)), 100_500.0],
        [session_close_ms_utc(date(2025, 1, 7)), 100_750.0],
    ]
    first, second = result.detail["trades"]
    assert math.isclose(first["pnl"], 500.0, abs_tol=1e-9, rel_tol=0) and first["exit_reason"] is None
    assert math.isclose(second["pnl"], -100.0, abs_tol=1e-9, rel_tol=0) and second["exit_reason"] == EXIT_AT_WINDOW_END


def test_a_summary_run_has_no_detail_and_an_undefined_statistic_reads_none() -> None:
    result = project_response(
        _request(),
        _response(statistics={"net_profit": 750.0, "net_profit_pct": 0.0075, "sharpe_ratio": float("nan"), "max_drawdown_pct": 0.004, "win_rate": 0.5}),
        strategy_key="ema_crossover_signal",
        initial_cash=100_000.0,
    )
    assert result.detail is None and result.metrics.sharpe_ratio is None


def test_a_summary_run_keeps_the_daily_curve_a_detail_run_of_the_same_point_holds() -> None:
    summary = project_response(_request(), _response(equity_curve=[]), strategy_key="ema_crossover_signal", initial_cash=100_000.0)
    detail = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0)

    assert summary.detail is None and detail.detail is not None
    assert summary.curve == detail.curve == [tuple(point) for point in detail.detail["daily_equity"]]


def test_a_day_end_the_calendar_refuses_costs_the_run_its_curve_and_never_its_result() -> None:
    saturday = et_midnight_ms(date(2025, 1, 11)) + 12 * 60 * 60 * 1000
    result = project_response(
        _request(), _response(equity_curve=[], equity_day_ends=[{"timestamp": saturday, "equity": 100_000.0}]), strategy_key="ema_crossover_signal", initial_cash=100_000.0
    )

    assert result.curve is None
    assert (result.metrics.status, result.metrics.net_profit) == ("completed", 750.0)


def test_a_run_the_engine_kept_no_day_ends_for_has_no_curve_and_keeps_its_result() -> None:
    result = project_response(_request(), _response(equity_curve=[], equity_day_ends=[]), strategy_key="ema_crossover_signal", initial_cash=100_000.0)

    # No curve, not an empty one: the run counts among those with no curve recorded.
    assert result.curve is None
    assert (result.metrics.status, result.metrics.net_profit) == ("completed", 750.0)


def test_a_failed_engine_run_is_a_failed_evaluation_with_its_reason() -> None:
    result = project_response(_request(detail=True), _response(success=False, error="no bars"), strategy_key="ema_crossover_signal", initial_cash=1.0)
    assert (result.metrics.status, result.metrics.error, result.detail, result.curve) == ("failed", "no bars", None, None)


# ── Durable detail readback (#3039) ─────────────────────────────────────


def _no_engine_call(request: EvaluationRequest) -> EvaluationResult:
    raise AssertionError("a durable detail readback must not execute the engine")


def _evaluator() -> StudyEvaluator:
    return StudyEvaluator(
        study_id="study-1",
        attempt=1,
        strategy_key="ema_crossover_signal",
        context_digest=context_digest(_context()),
        run_up_sessions=1,
        capability=EvaluationCapability(allowed=(JAN,)),
        budget_limit=10,
        execute=_no_engine_call,
    )


def _record(key: str, *, status: str = "completed", detail: object = None) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(evaluation_key=key, status=status, detail_json=detail)


def _patch_db_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the evaluator's DB calls through in-memory fakes (no pool, no event loop)."""
    from types import SimpleNamespace

    from app.research.golden_search import evaluator as evaluator_module

    async def fake_with_connection(fn, /, *args, **kwargs):
        return await fn(SimpleNamespace(), *args, **kwargs)

    def fake_run_sync(coroutine):
        import asyncio

        return asyncio.run(coroutine)

    monkeypatch.setattr(evaluator_module, "with_connection", fake_with_connection)
    monkeypatch.setattr(evaluator_module, "run_sync", fake_run_sync)


def _durable_rows(monkeypatch: pytest.MonkeyPatch, records: dict[str, object]) -> list[str]:
    """Replace the DB bridge with an in-memory evaluation store; returns the keys detail_of looked up."""
    import app.research.golden_search.repository as repository

    looked_up: list[str] = []

    async def fake_get_evaluation(conn, study_id, key):
        looked_up.append(key)
        return records.get(key)

    monkeypatch.setattr(repository, "get_evaluation", fake_get_evaluation)
    _patch_db_bridge(monkeypatch)
    return looked_up


def _detail_key(evaluator: StudyEvaluator, point: dict, window: tuple[int, int]) -> str:
    return evaluation_key(
        evaluator.context_digest,
        hash_point(evaluator.strategy_key, dict(point)),
        window,
        BASE_SCENARIO,
        True,
    )


def test_the_recorded_detail_shape_admits_what_detail_payload_writes_and_names_what_is_missing() -> None:
    recorded = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0).detail
    assert recorded is not None and detail_integrity_problem(recorded) is None
    assert detail_integrity_problem(None) == "no recorded detail payload"
    assert "initial_cash" in detail_integrity_problem({"daily_equity": [], "trades": []})
    assert "daily_equity" in detail_integrity_problem({"initial_cash": 1.0, "daily_equity": "gone", "trades": []})
    assert "trades" in detail_integrity_problem({"initial_cash": 1.0, "daily_equity": [[1, 2]], "trades": "gone"})
    assert "daily_equity" in detail_integrity_problem({"initial_cash": 1.0, "daily_equity": [[1, 2, 3]], "trades": []})
    # Corrupted scalars inside a structurally valid wrapper are named too —
    # the reading consumes these values, so a wrong-typed one is not evidence.
    assert "daily_equity" in detail_integrity_problem({"initial_cash": 1.0, "daily_equity": [["bad", 100.0]], "trades": []})
    assert "daily_equity" in detail_integrity_problem({"initial_cash": 1.0, "daily_equity": [[1, True]], "trades": []})
    assert "initial_cash" in detail_integrity_problem({"initial_cash": None, "daily_equity": [], "trades": []})
    assert "trades" in detail_integrity_problem({"initial_cash": 1.0, "daily_equity": [], "trades": [["not", "a", "mapping"]]})


def test_detail_of_reads_a_completed_evaluation_this_evaluator_never_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = _evaluator()
    payload = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0).detail
    looked_up = _durable_rows(monkeypatch, {_detail_key(evaluator, POINT, JAN): _record(_detail_key(evaluator, POINT, JAN), detail=payload)})

    first = evaluator.detail_of(POINT, window=JAN)
    second = _evaluator().detail_of(POINT, window=JAN)

    assert first == payload and second == payload
    assert looked_up == [_detail_key(evaluator, POINT, JAN)] * 2, "each fresh instance resolves the row durably"


def test_detail_of_cannot_substitute_a_summary_row_or_another_window(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = _evaluator()
    summary_key = evaluation_key(evaluator.context_digest, hash_point(evaluator.strategy_key, dict(POINT)), JAN, BASE_SCENARIO, False)
    other_window = (JAN[0], JAN[1] + DAY)
    payload = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0).detail
    looked_up = _durable_rows(monkeypatch, {
        summary_key: _record(summary_key, detail=payload),
        _detail_key(evaluator, POINT, other_window): _record(_detail_key(evaluator, POINT, other_window), detail=payload),
    })

    assert evaluator.detail_of(POINT, window=JAN) is None

    assert _detail_key(evaluator, POINT, JAN) in looked_up
    assert summary_key not in looked_up, "a detail request must resolve the detail row by its exact key"


def test_detail_of_keeps_missing_evidence_missing_and_surfaces_a_broken_payload_as_integrity(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = _evaluator()
    key = _detail_key(evaluator, POINT, JAN)
    payload = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0).detail
    rows: dict[str, object] = {}
    _durable_rows(monkeypatch, rows)

    assert evaluator.detail_of(POINT, window=JAN) is None, "a row that never ran is legitimate missing evidence"

    rows[key] = _record(key, status="failed", detail=None)
    assert evaluator.detail_of(POINT, window=JAN) is None, "a failed run's absent payload is legitimate missing evidence"

    for broken in (None, {"daily_equity": [], "trades": []}, {"initial_cash": 1.0, "daily_equity": "gone", "trades": []}):
        rows[key] = _record(key, status="completed", detail=broken)
        with pytest.raises(EvidenceIntegrityError, match=key[:12]):
            evaluator.detail_of(POINT, window=JAN)

    rows[key] = _record(key, status="completed", detail=payload)
    assert evaluator.detail_of(POINT, window=JAN) == payload


def test_final_chance_propagates_a_durable_evidence_integrity_failure() -> None:
    """A corrupt durable detail fails the exam; it must not read as a failed reading component (#3039 review).

    ``_final_chance_for`` converts diagnostic exceptions into recorded failed
    readings so a reading bug never fails an exam — but corrupt durable
    evidence is not a reading bug: an exam that concludes over it would use
    the very bytes the integrity contract refuses.
    """
    from types import SimpleNamespace

    from app.research.golden_search.selection import Metrics
    from app.research.golden_search.stages import _final_chance_for

    class _CorruptDetailEvaluator:
        context_digest = "digest"

        def detail_of(self, point, *, window):
            raise EvidenceIntegrityError("evaluation k: no recorded detail payload")

    ctx = SimpleNamespace(
        protocol=SimpleNamespace(policy=SimpleNamespace(objective="net_profit")),
        final=(0, 1),
        development=(0, 1),
        cancel_check=lambda: None,
    )
    completed = Metrics(status="completed", total_trades=1, net_profit=1.0, total_return_pct=0.01, sharpe_ratio=1.0, max_drawdown_pct=0.01, win_rate=1.0)
    fit = {"point": {"symbol": "SPY"}, "point_hash": "h", "metrics": completed.as_dict()}

    with pytest.raises(EvidenceIntegrityError, match="no recorded detail payload"):
        _final_chance_for(ctx, _CorruptDetailEvaluator(), fit, completed, None)


# ── The ordered ready-batch seam (#3040) ─────────────────────────────────


def _metrics_by_gap(gap: float) -> Metrics:
    return Metrics(
        status="completed", total_trades=1, net_profit=gap, total_return_pct=0.01,
        sharpe_ratio=1.0, max_drawdown_pct=0.01, win_rate=1.0,
    )


def _point(gap: float) -> dict:
    return {**POINT, "gap": gap}


class _EngineSpy:
    """A fake engine whose result is the point's gap, recording execution order."""

    def __init__(self, fail_on_gap: float | None = None, cancel_on_gap: float | None = None) -> None:
        self.ran: list[float] = []
        self.fail_on_gap = fail_on_gap
        self.cancel_on_gap = cancel_on_gap

    def __call__(self, request: EvaluationRequest) -> EvaluationResult:
        gap = float(request.point["gap"])
        self.ran.append(gap)
        if self.cancel_on_gap is not None and gap == self.cancel_on_gap:
            from app.jobs.progress import JobCancelled

            raise JobCancelled("cancelled mid-batch")
        if self.fail_on_gap is not None and gap == self.fail_on_gap:
            raise RuntimeError("engine exploded")
        return EvaluationResult(metrics=_metrics_by_gap(gap))


class _ReservationBridge:
    """The evaluator's persistence calls, in memory: fenced rows, a budget and a call log.

    Mirrors ``repository.reserve_evaluation``'s contract: a completed row
    answers ``cached`` without a budget unit, a pending row of this attempt
    re-admits without a new unit, and ``release`` leaves the row pending and
    marked (``repository.release_evaluation``), never deleted.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.consumed = 0
        self.rows: dict[str, dict] = {}
        self.reserved: list[str] = []
        self.completed: list[str] = []
        self.released: list[str] = []

    def precomplete(self, key: str, metrics: Metrics) -> None:
        self.rows[key] = {"status": "completed", "attempt": 0, "detail": None, "metrics": metrics}

    async def reserve(self, conn, study_id, attempt, new, *, limit, retry_allowance):
        from types import SimpleNamespace

        self.reserved.append(new.evaluation_key)
        row = self.rows.get(new.evaluation_key)
        if row is not None and row["status"] != "pending":
            return SimpleNamespace(
                kind="cached",
                counted_as_cache_hit=True,
                record=SimpleNamespace(
                    status=row["status"], attempt=row["attempt"], detail_json=row["detail"], metrics=row["metrics"]
                ),
            )
        if row is None:
            if self.consumed + 1 > self.limit:
                return SimpleNamespace(kind="exhausted")
            self.consumed += 1
            self.rows[new.evaluation_key] = {"status": "pending", "attempt": attempt, "detail": None, "metrics": None}
        return SimpleNamespace(kind="run")

    async def complete(self, conn, study_id, attempt, key, *, metrics, detail, curve):
        self.completed.append(key)
        self.rows[key] = {"status": "completed", "attempt": attempt, "detail": detail, "metrics": metrics}

    async def release(self, conn, study_id, attempt, key):
        self.released.append(key)
        if key in self.rows and self.rows[key]["status"] == "pending":
            self.rows[key]["cancelled"] = True


def _seamed_evaluator(
    monkeypatch: pytest.MonkeyPatch, engine: _EngineSpy, bridge: _ReservationBridge, *, dispatch=None
) -> StudyEvaluator:
    """An evaluator over the in-memory bridge, sharing the #3039 tests' DB patch."""
    import app.research.golden_search.repository as repository

    monkeypatch.setattr(repository, "reserve_evaluation", bridge.reserve)
    monkeypatch.setattr(repository, "complete_evaluation", bridge.complete)
    monkeypatch.setattr(repository, "release_evaluation", bridge.release)
    monkeypatch.setattr(repository, "metrics_of", lambda record: record.metrics)
    _patch_db_bridge(monkeypatch)
    return StudyEvaluator(
        study_id="study-1",
        attempt=1,
        strategy_key="ema_crossover_signal",
        context_digest=context_digest(_context()),
        run_up_sessions=1,
        capability=EvaluationCapability(allowed=(JAN,)),
        budget_limit=bridge.limit,
        execute=engine,
        dispatch=dispatch,
    )


def _key_of(evaluator: StudyEvaluator, point: Mapping[str, object], *, detail: bool = False) -> str:
    return evaluation_key(
        evaluator.context_digest,
        hash_point(evaluator.strategy_key, dict(point)),
        JAN,
        BASE_SCENARIO,
        detail,
    )


def test_the_serial_adapter_reserves_executes_and_persists_in_logical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    progress: list[int] = []
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    evaluator.on_evaluated = progress.append

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3], "results come back in input order"
    assert engine.ran == [0.1, 0.2, 0.3], "the serial adapter executes the batch in logical order"
    assert bridge.reserved == bridge.completed, "every reservation is completed, in reservation order"
    assert evaluator.new_runs == 3 and evaluator.cache_hits == 0
    assert progress == [1, 2, 3]


def test_out_of_order_delivery_c_a_b_is_consumed_as_a_b_c(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delivery order never reaches the caller: C,A,B arrives and A,B,C is what evaluate returns."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=reordered_dispatch([2, 0, 1]))

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3]
    assert set(bridge.completed) == set(bridge.reserved), "every admitted evaluation was persisted, in whatever order it arrived"
    assert evaluator.new_runs == 3


def test_a_budget_for_two_executes_a_and_b_then_reports_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.golden_search.zoom import BudgetExhausted

    engine, bridge = _EngineSpy(), _ReservationBridge(limit=2)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)

    with pytest.raises(BudgetExhausted):
        evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert engine.ran == [0.1, 0.2], "the affordable prefix ran before exhaustion was reported"
    assert len(bridge.reserved) == 3, "the third reservation is the one that answers exhausted"
    assert set(bridge.completed) == {_key_of(evaluator, _point(gap)) for gap in (0.1, 0.2)}


def test_the_prefix_stops_at_the_first_unaffordable_identity_without_speculation(monkeypatch: pytest.MonkeyPatch) -> None:
    """No searching past the first unaffordable new identity — even when a later point is cached."""
    from app.research.golden_search.zoom import BudgetExhausted

    engine, bridge = _EngineSpy(), _ReservationBridge(limit=1)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    cached_key = _key_of(evaluator, _point(0.3))
    bridge.precomplete(cached_key, _metrics_by_gap(0.3))

    with pytest.raises(BudgetExhausted):
        evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert engine.ran == [0.1], "only the affordable prefix executed"
    assert cached_key not in bridge.reserved, "the cached later point was never even probed for cheaper work"
    assert evaluator.cache_hits == 0


def test_duplicates_join_one_execution_but_distinct_identities_stay_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)

    metrics = evaluator.evaluate([_point(0.1), _point(0.1), _point(0.2)], window=JAN, stage="search")
    assert [m.net_profit for m in metrics] == [0.1, 0.1, 0.2]
    assert engine.ran == [0.1, 0.2] and len(bridge.reserved) == 2, "one full key joins one execution and one unit"

    # The same point under a different identity (summary vs detail) is separate
    # work: its own reservation, its own execution, its own budget unit.
    evaluator.evaluate([_point(0.1)], window=JAN, stage="exam", detail=True)
    assert len(bridge.reserved) == 3 and len(set(bridge.reserved)) == 3
    assert evaluator.new_runs == 3 and evaluator.cache_hits == 0


def test_a_cancelled_row_re_admits_without_a_new_budget_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A released row re-runs free, exactly as ``reserve_evaluation`` re-admits it (#3040)."""
    engine, bridge = _EngineSpy(cancel_on_gap=0.1), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)

    from app.jobs.progress import JobCancelled

    with pytest.raises(JobCancelled, match="cancelled mid-batch"):
        evaluator.evaluate([_point(0.1)], window=JAN, stage="search")
    assert bridge.consumed == 1, "the reservation charged one unit"

    engine.cancel_on_gap = None
    metrics = evaluator.evaluate([_point(0.1)], window=JAN, stage="search")
    assert metrics[0].net_profit == 0.1 and bridge.consumed == 1, "the re-admission charged nothing new"


def test_cached_rows_are_served_during_reservation_and_consume_the_same_units(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    bridge.precomplete(_key_of(evaluator, _point(0.1)), _metrics_by_gap(0.1))

    progress: list[int] = []
    evaluator.on_evaluated = progress.append
    metrics = evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2]
    assert engine.ran == [0.2], "the cached identity never executed"
    assert evaluator.cache_hits == 1 and evaluator.new_runs == 1
    assert bridge.consumed == 1, "the cached row consumed no budget unit"
    assert progress == [1, 2]


def test_cancellation_releases_the_cancelled_and_every_never_ran_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.jobs.progress import JobCancelled

    engine, bridge = _EngineSpy(cancel_on_gap=0.2), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)

    with pytest.raises(JobCancelled, match="cancelled mid-batch"):
        evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert bridge.completed == [_key_of(evaluator, _point(0.1))], "work before the cancellation is recorded"
    assert set(bridge.released) == {_key_of(evaluator, _point(0.2)), _key_of(evaluator, _point(0.3))}, (
        "the cancelled reservation and the never-ran members are released, like the interleaved loop left them unreserved"
    )
    assert evaluator.new_runs == 1


def test_an_adapter_that_raises_a_cancellation_releases_every_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract says deliver, never raise — but a raised one still leaves no pending trap."""
    from app.jobs.progress import JobCancelled

    engine, bridge = _EngineSpy(cancel_on_gap=0.2), _ReservationBridge(limit=10)

    def raising_dispatch(run, batch):
        return [serial_dispatch(run, [batch[0]])[0], *[  # second item raises out of the adapter
            DeliveredOutcome(key=item.key, result=run(item.request)) for item in batch[1:]
        ]]

    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=raising_dispatch)

    with pytest.raises(JobCancelled, match="cancelled mid-batch"):
        evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert set(bridge.released) == {
        _key_of(evaluator, _point(gap)) for gap in (0.1, 0.2, 0.3)
    }, "an escaping cancellation releases the whole admitted batch"


def test_a_failed_engine_run_is_recorded_as_evidence_and_the_batch_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(fail_on_gap=0.2), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert engine.ran == [0.1, 0.2, 0.3]
    assert [m.status for m in metrics] == ["completed", "failed", "completed"]
    assert "engine exploded" in (metrics[1].error or "")
    assert len(bridge.completed) == 3, "a failed run is persisted like any result"


def test_capability_is_checked_before_any_reservation_or_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    outside = (et_midnight_ms(date(2025, 2, 1)), et_midnight_ms(date(2025, 3, 1)))

    with pytest.raises(CapabilityError):
        evaluator.evaluate([_point(0.1)], window=outside, stage="search")

    assert bridge.reserved == [] and engine.ran == []
