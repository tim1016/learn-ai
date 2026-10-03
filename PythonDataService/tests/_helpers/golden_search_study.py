"""Builders for Golden Search study-service tests: a seeded lake, a small EMA plan, a fake engine and a fake approval.

The fake engine stands in for ``execute_engine_backtest`` at the study
evaluator's one injection point; nothing here calls the real engine. Its
landscape is a smooth function of the searched knobs, so Zoom moves are
predictable, and a test can make it depend on the window to probe leakage.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search import service
from app.research.golden_search.evaluator import EvaluationRequest, EvaluationResult
from app.research.golden_search.models import StudyRow
from app.research.golden_search.selection import Metrics
from app.research.golden_search.stages import ApprovalBinding, StageOutcome
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests._helpers.lean_store import seed_store_day

DEVELOPMENT = (date(2025, 1, 1), date(2025, 4, 1))
FINAL = (date(2025, 4, 1), date(2025, 5, 1))
LAKE_FROM = date(2024, 12, 2)
CAPITAL = 100_000.0


def unique_symbol() -> str:
    return "G" + uuid.uuid4().hex[:6].upper()


def clean_identity() -> CodeIdentity:
    """This process's code identity, labelled clean: Finish refuses a study locked from a dirty tree,
    and a developer running these tests mid-edit should not see that rule instead of the one under test."""
    return dataclasses.replace(resolve_code_identity(), tree_state="clean")


def seed_lake(root: Path, symbol: str, *, start: date = LAKE_FROM, end: date = FINAL[1] - timedelta(days=1)) -> Path:
    """Thin deterministic minute zips for every session — enough for availability and a snapshot."""
    for day in expected_sessions(start, end):
        seed_store_day(root, symbol, day, count=30)
    return root


def window_ms(window: tuple[date, date]) -> tuple[int, int]:
    return et_midnight_ms(window[0]), et_midnight_ms(window[1])


def registry_params(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


def plan_request(symbol: str, **overrides: Any) -> dict[str, Any]:
    """A small lockable EMA plan: gap and hold searched by a 3-point, 1-refinement, 1-pass Zoom; two 1/1-month folds."""
    searched = {"gap": (0.0, 0.6, 0.05), "hold_bars": (2.0, 12.0, 1.0)}
    fixed = {"rsi_min": 50.0, "rsi_max": 70.0, "fast_period": 5.0, "slow_period": 10.0, "gap_bps": 0.0}
    knobs = []
    for name in ("gap", "rsi_min", "rsi_max", "fast_period", "slow_period", "hold_bars", "gap_bps"):
        if name in searched:
            low, high, step = searched[name]
            knobs.append({"name": name, "mode": "search", "low": low, "high": high, "fixed_value": 0.0, "step": step})
        else:
            knobs.append({"name": name, "mode": "fixed", "low": 0.0, "high": 0.0, "fixed_value": fixed[name], "step": None})
    dev_start, dev_end = window_ms(DEVELOPMENT)
    final_start, final_end = window_ms(FINAL)
    request: dict[str, Any] = {
        "strategy_key": "ema_crossover_signal",
        "symbol": symbol,
        "method": "zoom",
        "knobs": knobs,
        "seed": None,
        "incumbent": {"source": "registry", "qualification_id": None, "params": registry_params(symbol)},
        "policy": {"objective": "sharpe_ratio", "min_trades": 1, "max_drawdown_ceiling": 0.5, "require_positive_net": True},
        "zoom": {"points": 3, "refinements": 1, "passes": 1},
        "development_start_ms": dev_start,
        "development_end_ms": dev_end,
        "final_start_ms": final_start,
        "final_end_ms": final_end,
        "training_months": 1,
        "test_months": 1,
        "recent_window": True,
        "pair_audits": [["gap", "hold_bars"]],
        "neighbor_audit": True,
        "stress": [
            {"key": "slippage_1c", "label": "Extra 1¢/share slippage", "slippage_add": 0.01, "commission_add": 0.0, "fill_mode": None},
            {"key": "commission_1", "label": "Extra $1 per order", "slippage_add": 0.0, "commission_add": 1.0, "fill_mode": None},
        ],
        "execution": {"fill_mode": "decision_minute_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": CAPITAL},
        "exam_min_trades": 1,
        "budget_cap": 5000,
    }
    request.update(overrides)
    return request


def frequency_plan_request(symbol: str, rate: int = 50, **overrides: Any) -> dict[str, Any]:
    """:func:`plan_request` under an expected trade frequency: the rate set, both fixed floors absent (ADR 0074)."""
    request = plan_request(symbol, expected_trades_per_year=rate, exam_min_trades=None, **overrides)
    request["policy"] = {**request["policy"], "min_trades": None}
    return request


Score = Callable[[Mapping[str, Any], tuple[int, int], str], float]


def smooth_score(point: Mapping[str, Any], window: tuple[int, int], scenario: str) -> float:
    """Peaks at gap 0.35, hold 7 whatever the window; stress costs shave a little."""
    gap = float(point.get("gap", 0.2))
    hold = float(point.get("hold_bars", 5))
    cost = {"base": 0.0, "slippage_1c": 0.05, "commission_1": 0.02}.get(scenario, 0.0)
    return 2.0 - 8.0 * (gap - 0.35) ** 2 - ((hold - 7.0) / 4.0) ** 2 - cost


@dataclass
class FakeEngine:
    """An ``ExecuteBacktest`` whose Sharpe is ``score(point, window, scenario)``; records every request."""

    score: Score = smooth_score
    calls: list[EvaluationRequest] = field(default_factory=list)
    crash_on_call: int | None = None

    def __call__(self, request: EvaluationRequest) -> EvaluationResult:
        self.calls.append(request)
        if self.crash_on_call is not None and len(self.calls) == self.crash_on_call:
            raise SimulatedCrash("the worker died mid-evaluation")
        scenario = "base" if request.scenario is None else request.scenario.key
        sharpe = self.score(request.point, request.window, scenario)
        metrics = Metrics(
            status="completed",
            total_trades=40,
            net_profit=1_000.0 * sharpe,
            total_return_pct=sharpe / 100.0,
            sharpe_ratio=sharpe,
            max_drawdown_pct=0.1,
            win_rate=0.5,
        )
        return EvaluationResult(metrics=metrics, detail=fake_detail(request.window, sharpe) if request.detail else None)


class SimulatedCrash(BaseException):
    """A process death: nothing in the stage may catch it."""


def fake_detail(window: tuple[int, int], sharpe: float) -> dict[str, Any]:
    days = expected_sessions(et_date_at_ms(window[0]), et_date_at_ms(window[1] - 1))
    final = CAPITAL * (1.0 + sharpe / 100.0)
    daily = [[session_close_ms_utc(day), CAPITAL + (final - CAPITAL) * (index + 1) / len(days)] for index, day in enumerate(days)]
    # Two trades that add up to the run's net profit (1,000 x Sharpe at no commission), as the engine's do.
    net = final - CAPITAL
    trades = []
    for index, share in enumerate((0.7, 0.3)):
        exit_ms = session_close_ms_utc(days[-1 - index]) - 60_000
        trades.append(
            {
                "entry_ms": exit_ms - 3_600_000,
                "exit_ms": exit_ms,
                "entry_price": 500.0,
                "exit_price": 501.0,
                "quantity": 10,
                "pnl": net * share,
                "pnl_pct": 0.002,
                "indicators": {"ema5": 1.0, "ema10": 0.5, "rsi": 55.0},
                "exit_reason": None,
            }
        )
    return {"initial_cash": CAPITAL, "daily_equity": daily, "trades": trades[::-1]}


# ── A fake of the approval workflow's interface ──────────────────────────


@dataclass(frozen=True, kw_only=True)
class FakeApprovalRequest:
    study_id: str
    strategy_key: str
    symbol: str
    candidate_point: dict[str, Any]
    proof_window: Any
    snapshot: Any
    roots: tuple[Path, ...]
    execution: Any
    research: dict[str, Any]
    note: str
    expected_default_qualification_id: str | None
    actor: str = "owner"


@dataclass(frozen=True)
class FakeCheckpoint:
    run_id: int | None = None
    proof: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "proof": self.proof}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> FakeCheckpoint:
        return cls() if payload is None else cls(run_id=payload.get("run_id"), proof=payload.get("proof"))


@dataclass(frozen=True)
class FakeOutcome:
    status: str
    qualification_id: str | None
    failure_code: str | None
    failure_reason: str | None


@dataclass
class FakeApproval:
    """Consumes the proof reservation and publishes (or fails) the way the frozen interface describes."""

    fail_with: tuple[str, str] | None = None
    requests: list[FakeApprovalRequest] = field(default_factory=list)
    checkpoints: list[FakeCheckpoint] = field(default_factory=list)

    def binding(self) -> ApprovalBinding:
        return ApprovalBinding(request_type=FakeApprovalRequest, checkpoint_from_dict=FakeCheckpoint.from_dict, approve=self.approve)

    def approve(self, request: FakeApprovalRequest, *, checkpoint, save_checkpoint, consume_reserved, on_commit, cancel_check=lambda: None, blob_store=None) -> FakeOutcome:
        self.requests.append(request)
        self.checkpoints.append(checkpoint)
        if checkpoint.proof is None:
            consume_reserved("proof", 2)
            checkpoint = FakeCheckpoint(proof={"trace_root": "t" * 64})
            save_checkpoint(checkpoint)
        if self.fail_with is not None:
            return FakeOutcome("failed", None, *self.fail_with)
        if checkpoint.run_id is None:
            consume_reserved("run", 1)
            checkpoint = FakeCheckpoint(run_id=7, proof=checkpoint.proof)
            save_checkpoint(checkpoint)
        qualification_id = "q" + request.study_id[:31]

        async def publish(conn: Any) -> None:
            async with conn.transaction():
                await on_commit(conn, qualification_id)

        run_sync(with_connection(publish))
        return FakeOutcome("approved", qualification_id, None, None)


# ── Driving a study ──────────────────────────────────────────────────────


@dataclass
class Driver:
    """Commands and stages for one study, as the HTTP layer and jobs boundary would issue them."""

    roots: list[Path]
    engine: FakeEngine = field(default_factory=FakeEngine)
    approval: FakeApproval = field(default_factory=FakeApproval)
    live: bool = False
    _keys: int = 0

    def liveness(self, job_id: str | None) -> bool | None:
        return self.live

    def key(self) -> str:
        self._keys += 1
        return f"k-{uuid.uuid4().hex[:8]}-{self._keys}"

    async def lock(self, symbol: str, *, rate: int | None = None, **overrides: Any) -> StudyRow:
        """Lock :func:`plan_request`, or :func:`frequency_plan_request` at ``rate`` when one is given."""
        request = plan_request(symbol, **overrides) if rate is None else frequency_plan_request(symbol, rate, **overrides)
        return await service.lock_study(request, idempotency_key=self.key(), roots=self.roots, identity=clean_identity())

    async def lock_and_run(self, symbol: str, *, key: str | None = None, **overrides: Any) -> service.CommandOutcome:
        """Run research (#2811): lock :func:`plan_request` and authorize Search with the intent to reach Compare."""
        return await service.lock_and_run(
            plan_request(symbol, **overrides), idempotency_key=key or self.key(), roots=self.roots, identity=clean_identity(), liveness=self.liveness
        )

    async def command(self, row: StudyRow, command: str, payload: Mapping[str, Any] | None = None, **kwargs: Any) -> service.CommandOutcome:
        return await service.run_command(
            row.id,
            command=command,
            expected_revision=row.revision,
            idempotency_key=kwargs.pop("idempotency_key", None) or self.key(),
            payload=payload or {},
            roots=self.roots,
            liveness=self.liveness,
            **kwargs,
        )

    async def run(self, outcome: service.CommandOutcome, *, job_id: str | None = None, **kwargs: Any) -> StageOutcome:
        assert outcome.dispatch is not None
        token = outcome.dispatch["payload"]["stage_token"]
        job = job_id or f"job-{uuid.uuid4().hex[:8]}"
        await service.bind_dispatch(outcome.study.id, stage_token=token, job_id=job)
        return await asyncio.to_thread(
            service.run_stage,
            outcome.study.id,
            stage_token=token,
            job_id=job,
            execute=kwargs.pop("execute", self.engine),
            approval=self.approval.binding(),
            roots=self.roots,
            **kwargs,
        )

    async def advance(self, row: StudyRow, command: str, payload: Mapping[str, Any] | None = None) -> StudyRow:
        outcome = await self.command(row, command, payload)
        if outcome.dispatch is not None:
            await self.run(outcome)
        return await service.get_row(outcome.study.id)

    async def to_candidate(self, symbol: str, **overrides: Any) -> StudyRow:
        row = await self.lock(symbol, **overrides)
        row = await self.advance(row, "continue")
        return await self.advance(row, "continue")

    async def detail(self, row: StudyRow) -> dict[str, Any]:
        return await service.detail(await service.get_row(row.id), liveness=self.liveness)
