"""Does the EMA strategy's verdict survive the terms a live bot trades on? (#2466)

Reruns the sealed ``ema_crossover_signal`` point (SPY, 15-minute bars, the
registry's ``validated_settings``) through the repository's own research
entry points, once under the research defaults and once per live execution
term, alone and combined, over the SAME windows:

* the Backtest Evidence Grade (``run_verdict``) of one engine run per window —
  ``execute_engine_backtest``, the entry point Strategy Lab uses;
* the walk-forward study verdict — the study's own fold planner and run-up
  (``walk_forward_study.service.prepare_launch``), Grid Search's own per-fold
  preflight and cell executor (``grid_search.service.prepare_launch``,
  ``engine_adapter.default_execute_cell``), the ranking contract's ``leader``
  and the frozen ``compute_verdict``. Only the database writes are skipped.

Two seams are replaced, and each replacement is the finding it measures:

* **Data roots.** Every managed lake read is admitted against the Postgres
  catalog (``app.data_lake.admission``), and this script may not touch the
  shared database. It therefore stages the lake's SPY minute archives into a
  plain LEAN tree after checking each against its corporate-action receipt
  (``verify_adjustment_receipt``) and points ``_resolve_lean_data_roots`` at
  it. Every engine read is then bound to the study's snapshot manifest, as a
  walk-forward cell's is.
* **Engine construction.** ``EngineBacktestRequest`` and ``GridSearchSpec``
  expose fill mode, a flat commission per order, slippage per share and
  initial cash. A fixed share quantity, Alpaca's regulatory fees and a fill
  at the open of the decision minute have no parameter, so
  ``_build_backtest_engine`` is wrapped to install them through the engine's
  own ``sizing_model`` / ``fill_model`` seams.

The script refuses to write numbers it cannot vouch for: it must reproduce
Strategy Lab's recorded runs 18 and 33 exactly (``RECORDED_RUN_18``,
``RECORDED_RUN_33``), and every run must show that each seam its terms replace
was consulted (``_require_seams_used``) and, where a grade row can show it,
took effect (``_require_terms_in_output``).

It is committed rather than kept as a scratch file because the follow-ups to
#2466 each end by rerunning it with one seam replacement deleted and the term
expressed through a request parameter instead.

Usage (host venv; no services, no vendor fetch)::

    POLYGON_API_KEY="" DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/measure_verdict_under_live_terms.py \\
        --lake-volume /abs/path/data-lake-volume --workdir /abs/scratch --out /abs/scratch/results.json

Reference: issue https://github.com/tim1016/learn-ai/issues/2466; findings in
``docs/references/ema-verdict-under-live-terms.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app.broker.alpaca.regulatory_fees import (  # noqa: E402
    FillFees,
    fees_for_fill,
    rates_for,
    settle_session,
)
from app.broker.contract.models import OrderSide  # noqa: E402
from app.data_lake.adjustment_versions import verify_adjustment_receipt  # noqa: E402
from app.data_lake.path_policy import lake_subpath  # noqa: E402
from app.engine.data.trade_bar import TradeBar  # noqa: E402
from app.engine.execution.fill_model import FillModel  # noqa: E402
from app.engine.execution.order import Direction, FillMode, Order, OrderEvent  # noqa: E402
from app.engine.strategy.registry import _STRATEGY_REGISTRY  # noqa: E402
from app.research.grid_search import engine_adapter  # noqa: E402
from app.research.grid_search import service as sweeps  # noqa: E402
from app.research.grid_search.models import CellResult, GridSearchSpec, NewSearch, SearchRow  # noqa: E402
from app.research.sweep.grid import StrategyGridConfig, expand_grid  # noqa: E402
from app.research.sweep.identity import CodeIdentity  # noqa: E402
from app.research.sweep.ranking import leader  # noqa: E402
from app.research.sweep.snapshot import DataSnapshot  # noqa: E402
from app.research.walk_forward_study import service as studies  # noqa: E402
from app.research.walk_forward_study.models import NewStudy, StudySpec  # noqa: E402
from app.research.walk_forward_study.verdict import FoldEvidence, compute_verdict  # noqa: E402
from app.schemas.engine_backtest import EngineBacktestRequest  # noqa: E402
from app.services import engine_backtest_service  # noqa: E402
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms  # noqa: E402

logger = logging.getLogger(__name__)

STRATEGY = "ema_crossover_signal"
SYMBOL = "SPY"
# The sealed live parameters (docs/audits/live-ema-spy-missed-entry-2026-09-17.md)
# are the registry's validated point; the script refuses to run if they drift.
EXPECTED_VALIDATED_SETTINGS = {"gap": 0.20, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}

# Grade windows: the two recorded Strategy Lab grades of this configuration
# (runs 18 and 33, artifacts/strategy-lab-validation-2026-09-27) and the whole
# walk-forward range.
GRADE_WINDOWS: dict[str, tuple[date, date]] = {
    "W3mo": (date(2026, 2, 2), date(2026, 4, 30)),
    "W6mo": (date(2025, 11, 3), date(2026, 4, 30)),
    "WF-range": (date(2024, 6, 3), date(2026, 8, 31)),
}
# Walk-forward study: the form's default 12-month training / 3-month test over
# the lake's SPY span (first archive 2024-05-20, which also holds the run-up).
STUDY_START = date(2024, 6, 1)
STUDY_END_EXCLUSIVE = date(2026, 9, 1)
TRAINING_MONTHS = 12
TEST_MONTHS = 3
MIN_TRADES = 5

RESEARCH_CASH = 100_000.0
RESEARCH_COMMISSION_PER_ORDER = 1.0
LIVE_QUANTITY = 1
# The budget the first budgeted Paper deploy set aside for one SPY share
# (2026-09-28, #2550); the bots on 2026-09-17..24 traded one share.
LIVE_BUDGET = 1_000.0
# CAT is pinned only from 2026-09-01. Earlier fills are charged the first
# pinned rate. For the one-share runs that is an upper bound (any rate below
# half a cent a share rounds up to one cent per trading day with fills); for
# research-size runs it assumes the unpinned earlier rate was no higher.
CAT_BACKDATE_FROM = date(2026, 9, 1)

# Strategy Lab's recorded grades of this configuration
# (artifacts/strategy-lab-validation-2026-09-27, gitignored): run 18 is W3mo on
# adjusted bars at $1 per order; run 33 is W6mo under us-equity-raw-ibkr-v1.
# Sharpe is not pinned: #2525 (236829fb) changed the daily-return convention
# after both were recorded.
RECORDED_RUN_18: dict[str, Any] = {
    "trades": 11,
    "net_profit": 136.3804,
    "total_fees": 22.0,
    "grade": "B",
    "composite": 59,
}
RECORDED_RUN_33: dict[str, Any] = {
    "trades": 20,
    "net_profit": 2696.8751,
    "total_fees": 40.0,
    "grade": "A",
    "composite": 81,
}

FillTiming = Literal["signal_bar_close", "next_bar_open", "decision_minute_open"]
FeeSchedule = Literal["flat_per_order", "alpaca_regulatory", "none"]


@dataclass(frozen=True)
class Terms:
    """One execution basis: the research defaults, or some live terms swapped in."""

    name: str
    label: str
    fill: FillTiming = "signal_bar_close"
    fees: FeeSchedule = "flat_per_order"
    slippage_per_share: float = 0.0
    fixed_quantity: int | None = None
    initial_cash: float = RESEARCH_CASH

    @property
    def request_fill_mode(self) -> str:
        return "next_bar_open" if self.fill == "next_bar_open" else "signal_bar_close"

    @property
    def commission_per_order(self) -> float:
        return RESEARCH_COMMISSION_PER_ORDER if self.fees == "flat_per_order" else 0.0


LIVE_FILL: FillTiming = "decision_minute_open"
RESEARCH = Terms("research", "Research defaults (100% equity, signal-bar close, $1/order, no spread, $100k)")
VARIANTS: tuple[Terms, ...] = (
    RESEARCH,
    Terms(
        "qty_1sh_1k",
        "T1: fixed 1 share on the $1,000 live budget",
        fixed_quantity=LIVE_QUANTITY,
        initial_cash=LIVE_BUDGET,
    ),
    Terms("qty_1sh_100k", "T1 (capital sensitivity): fixed 1 share on $100k", fixed_quantity=LIVE_QUANTITY),
    Terms(
        "fill_next_bar_open",
        "T2a: engine next_bar_open (open of the minute after the decision minute)",
        fill="next_bar_open",
    ),
    Terms("fill_decision_minute_open", "T2b: open of the decision minute (live proxy)", fill="decision_minute_open"),
    Terms("fees_alpaca", "T3: Alpaca regulatory fees instead of $1/order", fees="alpaca_regulatory"),
    Terms("spread_1c", "T4: $0.01/share per fill", slippage_per_share=0.01),
    Terms("spread_2c", "T4: $0.02/share per fill", slippage_per_share=0.02),
    Terms(
        "live_1c",
        "Combined live terms: 1 share on $1,000, decision-minute open, Alpaca fees, $0.01/share",
        fill=LIVE_FILL,
        fees="alpaca_regulatory",
        slippage_per_share=0.01,
        fixed_quantity=LIVE_QUANTITY,
        initial_cash=LIVE_BUDGET,
    ),
    Terms(
        "live_2c",
        "Combined live terms with $0.02/share",
        fill=LIVE_FILL,
        fees="alpaca_regulatory",
        slippage_per_share=0.02,
        fixed_quantity=LIVE_QUANTITY,
        initial_cash=LIVE_BUDGET,
    ),
    Terms(
        "live_1c_next_bar_open",
        "Combined live terms, engine next_bar_open instead of the decision-minute open",
        fill="next_bar_open",
        fees="alpaca_regulatory",
        slippage_per_share=0.01,
        fixed_quantity=LIVE_QUANTITY,
        initial_cash=LIVE_BUDGET,
    ),
    Terms(
        "live_2c_next_bar_open",
        "Combined live terms with $0.02/share, engine next_bar_open instead of the decision-minute open",
        fill="next_bar_open",
        fees="alpaca_regulatory",
        slippage_per_share=0.02,
        fixed_quantity=LIVE_QUANTITY,
        initial_cash=LIVE_BUDGET,
    ),
    Terms(
        "qty_1sh_1k_alpaca",
        "T1 + T3: fixed 1 share on $1,000 with Alpaca fees (no flat commission)",
        fees="alpaca_regulatory",
        fixed_quantity=LIVE_QUANTITY,
        initial_cash=LIVE_BUDGET,
    ),
    Terms(
        "params_only_proxy",
        "What the request parameters alone can express: $1,000 all-in (floors to 1 share), no commission, "
        "$0.01/share, engine next_bar_open",
        fill="next_bar_open",
        fees="none",
        slippage_per_share=0.01,
        initial_cash=LIVE_BUDGET,
    ),
    Terms(
        "costs_all_in",
        "Live costs and timing, research sizing (100% of $100k)",
        fill=LIVE_FILL,
        fees="alpaca_regulatory",
        slippage_per_share=0.01,
    ),
)


# ── Live terms the engine has no parameter for ───────────────────────────


class MeasurementRefused(RuntimeError):
    """A run did not measure what its variant claims; no numbers are written."""


@dataclass
class SeamUse:
    """How often each replaced seam was consulted since the counts were last taken.

    A replacement the engine never consulted would leave a variant measuring
    the research terms under a live-terms label, so every run takes these
    counts and refuses a zero (``_require_seams_used``).
    """

    sized: int = 0
    minute_open_fills: int = 0
    alpaca_fee_fills: int = 0

    def take(self) -> SeamUse:
        """These counts, zeroed for the next run."""
        taken = SeamUse(self.sized, self.minute_open_fills, self.alpaca_fee_fills)
        self.sized = self.minute_open_fills = self.alpaca_fee_fills = 0
        return taken


@dataclass
class FixedQuantitySizing:
    """``SetHoldings`` to a fixed share count — the live binding's ``quantity``."""

    quantity: int
    use: SeamUse
    name: str = "fixed_quantity"

    def target_quantity(
        self, *, portfolio_value: Decimal, price: Decimal, target_fraction: Decimal, order_fee: Decimal
    ) -> int:
        self.use.sized += 1
        return self.quantity if target_fraction > 0 else 0


class LiveTermsFillModel(FillModel):
    """The research fill model plus the two fill terms it cannot express.

    ``decision_minute_open``: a live run decides a 15-minute bucket on the
    minute bar that closes it and prices the order at that instant, so the
    order goes out early in the minute after the bucket. The backtest's lazy
    consolidator emits the bucket on that same minute; fill at its open. That
    is the zero-latency (optimistic) bound; ``next_bar_open`` is the
    one-minute (pessimistic) bound.
    ``alpaca_fees``: price each fill with the canonical Alpaca regulatory
    model, settled per ET trade date and component (rounded up to the cent),
    charged incrementally so each date's charges sum to its settlement.

    Only fills priced by ``fill_market_order`` see either term. The engine's
    force-flat, end-of-algorithm and bracket exits call ``compute_fee``
    directly (``engine.py:245``, ``:817``), which charges the flat commission —
    $0 in the Alpaca-fee variants. Force-flat is off and the strategy places no
    brackets, so that is at most the one end-of-algorithm exit per run. A
    market order from the final consolidated bar has no current minute
    (``engine.py:681``) and fills at the signal bar's close.
    """

    def __init__(
        self,
        *,
        mode: FillMode,
        slippage_per_share: Decimal,
        commission_per_order: Decimal,
        decision_minute_open: bool,
        alpaca_fees: bool,
        use: SeamUse,
    ) -> None:
        super().__init__(mode=mode, commission_per_order=commission_per_order, slippage_per_share=slippage_per_share)
        self.decision_minute_open = decision_minute_open
        self.alpaca_fees = alpaca_fees
        self.use = use
        self._accrued: dict[date, list[FillFees]] = {}

    def fill_market_order(
        self,
        order: Order,
        signal_bar: TradeBar,
        next_bar: TradeBar | None = None,
        current_bar: TradeBar | None = None,
    ) -> OrderEvent | None:
        if self.decision_minute_open and self.mode is FillMode.SIGNAL_BAR_CLOSE and current_bar is not None:
            event = self._fill_at_decision_minute_open(order, current_bar)
        else:
            event = super().fill_market_order(order, signal_bar, next_bar, current_bar)
        if event is not None and self.alpaca_fees:
            event.fee = self._alpaca_fee(event)
        return event

    def _fill_at_decision_minute_open(self, order: Order, current_bar: TradeBar) -> OrderEvent:
        self.use.minute_open_fills += 1
        price = current_bar.open
        if order.direction == Direction.LONG:
            price += self.slippage_per_share
        elif order.direction == Direction.SHORT:
            price -= self.slippage_per_share
        return OrderEvent(
            order_id=order.order_id,
            symbol=order.symbol,
            filled_at_ms=current_bar.start_ms,
            fill_price=price,
            fill_quantity=order.quantity,
            direction=order.direction,
            fee=self.compute_fee(quantity=int(order.quantity), fill_price=price),
            tag=order.tag,
        )

    def _alpaca_fee(self, event: OrderEvent) -> Decimal:
        self.use.alpaca_fee_fills += 1
        trade_date = et_date_at_ms(event.filled_at_ms)
        side = OrderSide.BUY if event.direction == Direction.LONG else OrderSide.SELL
        accrual = fees_for_fill(
            trade_date=trade_date, side=side, quantity=Decimal(abs(event.fill_quantity)), fill_price=event.fill_price
        )
        if accrual.cat is None:
            cat_rate = rates_for(CAT_BACKDATE_FROM).cat_per_share
            assert cat_rate is not None
            accrual = FillFees(sec=accrual.sec, taf=accrual.taf, cat=Decimal(abs(event.fill_quantity)) * cat_rate)
        session = self._accrued.setdefault(trade_date, [])
        before = settle_session(session).total if session else Decimal(0)
        session.append(accrual)
        return settle_session(session).total - before


@contextmanager
def live_terms(terms: Terms, roots: dict[bool, list[Path]]) -> Iterator[SeamUse]:
    """Point the engine at the staged roots and install the terms it has no parameter for."""
    original_roots = engine_backtest_service._resolve_lean_data_roots
    original_build = engine_backtest_service._build_backtest_engine
    use = SeamUse()

    def _roots(*, adjusted: bool) -> list[Path]:
        return roots[adjusted]

    def _build(**kwargs: Any) -> Any:
        engine = original_build(**kwargs)
        if terms.fill == "decision_minute_open" or terms.fees == "alpaca_regulatory":
            config = kwargs["execution_config"]
            engine.fill_model = LiveTermsFillModel(
                mode=config.fill_mode,
                slippage_per_share=config.slippage_per_share,
                commission_per_order=config.commission_per_order,
                decision_minute_open=terms.fill == "decision_minute_open",
                alpaca_fees=terms.fees == "alpaca_regulatory",
                use=use,
            )
        if terms.fixed_quantity is not None:
            engine.sizing_model = FixedQuantitySizing(terms.fixed_quantity, use)
        return engine

    engine_backtest_service._resolve_lean_data_roots = _roots  # type: ignore[assignment]
    engine_backtest_service._build_backtest_engine = _build  # type: ignore[assignment]
    try:
        yield use
    finally:
        engine_backtest_service._resolve_lean_data_roots = original_roots  # type: ignore[assignment]
        engine_backtest_service._build_backtest_engine = original_build  # type: ignore[assignment]


# ── Staging ──────────────────────────────────────────────────────────────


def stage_minute_archives(lake_volume: Path, workdir: Path) -> dict[str, Any]:
    """Copy SPY minute trade archives into plain LEAN trees, adjusted ones receipt-checked."""
    staged: dict[str, Any] = {}
    for mode, adjusted in (("polygon_split_adjusted", True), ("raw", False)):
        source = lake_volume / lake_subpath(mode) / "equity" / "usa" / "minute" / SYMBOL.lower()
        target_root = workdir / ("spy-adjusted" if adjusted else "spy-raw")
        target = target_root / "equity" / "usa" / "minute" / SYMBOL.lower()
        if target_root.exists():
            shutil.rmtree(target_root)
        target.mkdir(parents=True)
        digest = hashlib.sha256()
        archives = sorted(source.glob("*_trade.zip"))
        for archive in archives:
            payload = archive.read_bytes()
            sha = hashlib.sha256(payload).hexdigest()
            if adjusted:
                verify_adjustment_receipt(archive, sha, SYMBOL)
            (target / archive.name).write_bytes(payload)
            digest.update(f"{archive.name}:{sha}\n".encode())
        staged[mode] = {
            "root": str(target_root),
            "archives": len(archives),
            "first": archives[0].name[:8],
            "last": archives[-1].name[:8],
            "manifest_sha256": digest.hexdigest(),
        }
        logger.info("staged %s %s archives %s..%s", len(archives), mode, archives[0].name[:8], archives[-1].name[:8])
    return staged


# ── Runs ─────────────────────────────────────────────────────────────────


def _no_op(_: str) -> None:
    return None


def _grade_row(response: Any) -> dict[str, Any]:
    stats = response.statistics or {}
    verdict = response.run_verdict
    return {
        "success": response.success,
        "error": response.error,
        "trades": response.total_trades,
        "win_rate": response.win_rate,
        "net_profit": response.net_profit,
        "total_fees": response.total_fees,
        "initial_cash": response.initial_cash,
        "sharpe": stats.get("sharpe_ratio"),
        "cagr": stats.get("cagr"),
        "max_drawdown_pct": stats.get("max_drawdown_pct"),
        "profit_factor": stats.get("profit_factor"),
        "expectancy_pct": stats.get("expectancy_pct"),
        "psr": stats.get("probabilistic_sharpe_ratio"),
        "grade": verdict.grade if verdict else None,
        "composite": verdict.composite if verdict else None,
        "evidence_action": verdict.evidence_action if verdict else None,
        "verdict_status": verdict.status if verdict else None,
        "dimension_scores": {d.key: d.score for d in verdict.dimensions} if verdict else {},
        "max_quantity": max((t.quantity for t in response.trades), default=0),
    }


# ── Guards: refuse numbers the run cannot vouch for ─────────────────────


def _require_reproduced(name: str, row: dict[str, Any], recorded: dict[str, Any]) -> None:
    drift = {key: {"got": row[key], "recorded": want} for key, want in recorded.items() if row[key] != want}
    if drift:
        raise MeasurementRefused(f"{name} no longer reproduces its recorded run: {drift}")


def _require_seams_used(terms: Terms, use: SeamUse, *, trades: int, where: str) -> None:
    """Every seam ``terms`` replaces was consulted by a run that traded."""
    taken = use.take()
    replaced = {
        "sized": terms.fixed_quantity is not None,
        "minute_open_fills": terms.fill == "decision_minute_open",
        "alpaca_fee_fills": terms.fees == "alpaca_regulatory",
    }
    unused = [seam for seam, installed in replaced.items() if installed and getattr(taken, seam) == 0]
    if trades > 0 and unused:
        raise MeasurementRefused(f"{terms.name} {where}: {trades} trades, but the engine never consulted {unused}")


def _require_terms_in_output(terms: Terms, row: dict[str, Any], window: str) -> None:
    """The live terms a grade row shows directly: the share count, and a fee charged with no flat commission."""
    if not row["trades"]:
        return
    if terms.fixed_quantity is not None and row["max_quantity"] != terms.fixed_quantity:
        raise MeasurementRefused(
            f"{terms.name} {window}: traded up to {row['max_quantity']} shares, not the fixed {terms.fixed_quantity}"
        )
    flat_total = 2 * row["trades"] * RESEARCH_COMMISSION_PER_ORDER
    if terms.fees == "alpaca_regulatory" and (row["total_fees"] <= 0 or row["total_fees"] == flat_total):
        raise MeasurementRefused(
            f"{terms.name} {window}: fees {row['total_fees']} over {row['trades']} trades are not Alpaca's "
            f"regulatory fees (the flat commission is $0 here, and $1 per order would be {flat_total})"
        )


# ── Runs ─────────────────────────────────────────────────────────────────


def _grade_request(terms: Terms, window: str) -> EngineBacktestRequest:
    start, end = GRADE_WINDOWS[window]
    return EngineBacktestRequest(
        strategy_name=STRATEGY,
        params={"symbol": SYMBOL},
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        fill_mode=terms.request_fill_mode,
        commission_per_order=terms.commission_per_order,
        slippage_per_share=terms.slippage_per_share,
        initial_cash=terms.initial_cash,
        save_study=False,
        auto_fetch=False,
        summary_only=False,
    )


def grade_runs(terms: Terms, use: SeamUse, manifest: dict[str, str]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for window in GRADE_WINDOWS:
        started = time.monotonic()
        response = engine_backtest_service.execute_engine_backtest(
            request=_grade_request(terms, window), on_phase=_no_op, on_log=_no_op, data_manifest=manifest
        )
        rows[window] = {**_grade_row(response), "seconds": round(time.monotonic() - started, 1)}
        _require_seams_used(terms, use, trades=response.total_trades, where=window)
        _require_terms_in_output(terms, rows[window], window)
        if terms.name == "research" or window == "W3mo":
            rows[window]["trade_log"] = [
                {
                    "entry_ms": t.entry_time,
                    "exit_ms": t.exit_time,
                    "qty": t.quantity,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                }
                for t in response.trades
            ]
    return rows


def _row_from(record: NewSearch) -> SearchRow:
    """The launched sweep as its stored row would read back, without the database."""
    return SearchRow(
        id=record.id,
        owner=record.owner,
        strategy_key=record.strategy_key,
        symbol=record.symbol,
        status="running",
        attempt=1,
        job_id=None,
        created_at_ms=0,
        updated_at_ms=0,
        finished_at_ms=None,
        request=record.request,
        receipt=record.receipt,
        expected_cells=record.expected_cells,
        completed_cells=0,
        failed_cells=0,
        leader_params_hash=None,
        leader_params=None,
        incomplete=False,
        failure_reason=None,
    )


def launch_study(terms: Terms, roots: list[Path]) -> tuple[StudySpec, NewStudy]:
    """The study as ``prepare_launch`` freezes it: folds, run-up, one snapshot over the whole range."""
    grid = GridSearchSpec(
        strategy_key=STRATEGY,
        symbol=SYMBOL,
        param_ranges={},
        start_ms=et_midnight_ms(STUDY_START),
        end_ms=et_midnight_ms(STUDY_END_EXCLUSIVE),
        fill_mode=terms.request_fill_mode,
        commission_per_order=terms.commission_per_order,
        slippage_per_share=terms.slippage_per_share,
        initial_cash=terms.initial_cash,
        measure="sharpe_ratio",
        min_trades=MIN_TRADES,
    )
    spec = StudySpec(grid=grid, training_months=TRAINING_MONTHS, test_months=TEST_MONTHS)
    return spec, studies.prepare_launch(spec, job_id=None, roots=roots)


def walk_forward(terms: Terms, use: SeamUse, spec: StudySpec, study: NewStudy, roots: list[Path]) -> dict[str, Any]:
    """Train, select by the ranking contract, test, then the frozen verdict — the study's loop minus its writes."""
    grid = spec.grid
    snapshot = DataSnapshot.from_dict(study.receipt["data_snapshot"])
    identity = CodeIdentity(**study.receipt["code_identity"])

    def _cell(start_ms: int, end_ms: int, where: str) -> tuple[CellResult, dict[str, Any]]:
        record = sweeps.prepare_launch(
            spec.sweep_spec(start_ms, end_ms), job_id=None, roots=roots, snapshot=snapshot, identity=identity
        )
        row = _row_from(record)
        sweep_spec = GridSearchSpec.from_request_dict(record.request)
        candidates = list(
            expand_grid([StrategyGridConfig(sweep_spec.strategy_key, dict(sweep_spec.param_ranges))], [SYMBOL])
        )
        assert len(candidates) == 1, "the study sweeps the sealed point only"
        cell = engine_adapter.default_execute_cell(row, sweep_spec)(candidates[0])
        _require_seams_used(terms, use, trades=cell.total_trades, where=where)
        return cell, record.receipt["interval_table"]

    folds: list[dict[str, Any]] = []
    evidence: list[FoldEvidence] = []
    for fold in study.folds:
        train, train_window = _cell(fold.train_start_ms, fold.train_end_ms, f"fold {fold.fold_index} training")
        winner = leader([train], grid.measure, min_trades=grid.min_trades)
        test, test_window = _cell(fold.test_start_ms, fold.test_end_ms, f"fold {fold.fold_index} test")
        status: Literal["completed", "failed"] = (
            "completed" if winner is not None and test.status == "completed" else "failed"
        )
        item = FoldEvidence(
            fold_index=fold.fold_index,
            status=status,
            train_sharpe=train.sharpe_ratio,
            test_sharpe=test.sharpe_ratio,
            test_trades=test.total_trades,
        )
        evidence.append(item)
        folds.append(
            {
                "fold_index": fold.fold_index,
                "train": [
                    et_date_at_ms(train_window["evaluation_start_ms"]).isoformat(),
                    et_date_at_ms(train_window["evaluation_end_ms"]).isoformat(),
                ],
                "test": [
                    et_date_at_ms(test_window["evaluation_start_ms"]).isoformat(),
                    et_date_at_ms(test_window["evaluation_end_ms"]).isoformat(),
                ],
                "run_up_sessions": train_window["run_up_sessions"],
                "status": status,
                "train_sharpe": train.sharpe_ratio,
                "train_trades": train.total_trades,
                "train_net_profit": train.net_profit,
                "test_sharpe": test.sharpe_ratio,
                "test_trades": test.total_trades,
                "test_net_profit": test.net_profit,
                "retention": item.retention,
                "errors": [cell.error for cell in (train, test) if cell.error],
            }
        )
    verdict = compute_verdict(evidence, min_trades=grid.min_trades)
    return {"data_snapshot_digest": study.receipt["data_snapshot_digest"], "folds": folds, "verdict": verdict.as_dict()}


def _run_33_request() -> EngineBacktestRequest:
    """Recorded Strategy Lab run 33: W6mo, raw bars, the pinned us-equity-raw-ibkr-v1 profile."""
    start, end = GRADE_WINDOWS["W6mo"]
    return EngineBacktestRequest.model_validate(
        {
            "strategy_name": STRATEGY,
            "params": {"symbol": SYMBOL},
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "initial_cash": RESEARCH_CASH,
            "compatibility_profile": "us-equity-raw-ibkr-v1",
            "data_policy": {
                "source": "polygon",
                "symbol": SYMBOL,
                "adjusted": False,
                "session": "regular",
                "input_bars": {"timespan": "minute", "multiplier": 1},
                "strategy_bars": {"timespan": "minute", "multiplier": 1},
            },
            "save_study": False,
            "auto_fetch": False,
        }
    )


def reproduce_recorded_runs(roots: dict[bool, list[Path]]) -> dict[str, dict[str, Any]]:
    """Rerun Strategy Lab's recorded runs 18 and 33 on the research terms; refuse on any drift."""
    rows: dict[str, dict[str, Any]] = {}
    with live_terms(RESEARCH, roots):
        for name, request, recorded in (
            ("recorded_run_18", _grade_request(RESEARCH, "W3mo"), RECORDED_RUN_18),
            ("recorded_run_33", _run_33_request(), RECORDED_RUN_33),
        ):
            response = engine_backtest_service.execute_engine_backtest(request=request, on_phase=_no_op, on_log=_no_op)
            rows[name] = _grade_row(response)
            _require_reproduced(name, rows[name], recorded)
    return rows


def run_variant(terms: Terms, staged: dict[str, Any]) -> dict[str, Any]:
    logging.basicConfig(level=logging.WARNING)
    roots = {True: [Path(staged["polygon_split_adjusted"]["root"])], False: [Path(staged["raw"]["root"])]}
    started = time.monotonic()
    with live_terms(terms, roots) as use:
        spec, study = launch_study(terms, roots[True])
        wf = walk_forward(terms, use, spec, study, roots[True])
        grades = grade_runs(terms, use, study.receipt["data_snapshot"]["artifacts"])
    if terms == RESEARCH:
        _require_reproduced("research W3mo (recorded run 18)", grades["W3mo"], RECORDED_RUN_18)
    return {
        "terms": asdict(terms),
        "grades": grades,
        "walk_forward": wf,
        "seconds": round(time.monotonic() - started, 1),
    }


def _check_sealed_point() -> dict[str, Any]:
    registration = _STRATEGY_REGISTRY[STRATEGY]
    contract = registration.signal_program_contract
    assert contract is not None, f"{STRATEGY} has no signal-program contract"
    validated = dict(contract.validated_settings)
    defaults = registration.param_schema.model_validate({"symbol": SYMBOL}).model_dump()
    if validated != EXPECTED_VALIDATED_SETTINGS or any(defaults[k] != v for k, v in validated.items()):
        raise SystemExit(f"sealed point drifted: validated={validated} defaults={defaults}")
    return {"validated_settings": validated, "defaults": defaults}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lake-volume", type=Path, required=True, help="host path of the data-lake volume (read only)")
    parser.add_argument("--workdir", type=Path, required=True, help="scratch directory for the staged LEAN trees")
    parser.add_argument("--out", type=Path, required=True, help="JSON results path")
    parser.add_argument("--variants", nargs="*", default=[v.name for v in VARIANTS])
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="variants run in parallel processes; pass 1 under the macOS command sandbox, which refuses the pool",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    started = time.monotonic()
    sealed = _check_sealed_point()
    staged = stage_minute_archives(args.lake_volume, args.workdir)
    roots = {True: [Path(staged["polygon_split_adjusted"]["root"])], False: [Path(staged["raw"]["root"])]}
    recorded = reproduce_recorded_runs(roots)
    for name, row in recorded.items():
        logger.info(
            "%s reproduced: %s",
            name,
            json.dumps({k: row[k] for k in ("trades", "net_profit", "total_fees", "sharpe", "grade", "composite")}),
        )

    selected = [v for v in VARIANTS if v.name in set(args.variants)]
    results: dict[str, Any] = {}

    def _record(name: str, row: dict[str, Any]) -> None:
        results[name] = row
        logger.info(
            "%-24s WF=%-15s %s | %s (%ss)",
            name,
            row["walk_forward"]["verdict"]["label"],
            row["walk_forward"]["verdict"]["based_on"],
            " ".join(f"{w}:{g['grade']}/{g['composite']} ${g['net_profit']:.2f}" for w, g in row["grades"].items()),
            row["seconds"],
        )

    if args.jobs <= 1:
        for variant in selected:
            _record(variant.name, run_variant(variant, staged))
    else:
        # One process per variant: the engine gate admits one backtest per process.
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {v.name: pool.submit(run_variant, v, staged) for v in selected}
            for name, future in futures.items():
                _record(name, future.result())
    args.out.write_text(
        json.dumps(
            {
                "sealed_point": sealed,
                "staged": staged,
                "recorded_runs": recorded,
                "variants": results,
                "wall_seconds": round(time.monotonic() - started, 1),
            },
            indent=1,
            sort_keys=True,
            default=str,
        )
    )
    logger.info("wrote %s (%.0fs)", args.out, time.monotonic() - started)


if __name__ == "__main__":
    main()
