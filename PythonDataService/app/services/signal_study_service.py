"""Signal study (Research Lab → Signals → Engine): computes from admitted lake inputs (#3522).

The study's input contract. What it read before #3522 (the data-source census, #3491),
and what it reads now, field by field:

* **Bars.** Before: one Polygon ``fetch_aggregates`` call, trade aggregates for
  one stock symbol, in the size the request named. Now: the lake's one-minute
  trade bars, admitted through :mod:`app.research.study_inputs` (the existing
  materializer and committed-byte admission; the UI is never a writer), then
  folded to the requested size by ``fold_minute_bars`` — never the chart's
  resampler, which fills missing minutes forward.
* **Resolution.** Minute bars; 15 minutes by default (the route and the browser
  both pin it). The target looks ``SignalConfig.horizon`` = 15 *bars* ahead (the
  legacy ``compute_15min_forward_return`` wrapper), so 15 x the bar length in
  minutes: 225 minutes at the default. It is valid only when the forward bar
  falls on the same New York date, and the engine refuses a series with fewer
  than 70% valid targets. A day holds at most ``ceil(1440 / m)`` bars of ``m``
  minutes, so at most ``1 - 15 / that`` of them can have a target: bars longer
  than ``MAX_BAR_MINUTES`` minutes, hour bars and daily bars can never reach the
  gate. They failed the run every time and are now refused up front with a
  reason (#3527).
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only), never set
  explicitly. Now: the lake's ``polygon_split_adjusted`` mode, named in the
  identity. Prices sit on the lake's 1/10,000 grid (rounded half-up).
* **Session.** Before and now: **extended hours** — every bar the provider
  served, 04:00 to 20:00 ET. The browser sends ``session: "rth"`` and the
  backend never read it; that label stays inert so this migration does not
  silently change the sample. The session is now an explicit identity field
  (``session_policy``, default ``extended``); asking for ``regular`` is opt-in.
  Which session the study *should* use is an owner decision (D02), and it is
  not neutral: on regular-hours 15-minute bars (26 per session) a 15-bar target
  is valid for at most 11 of them (42%), under the engine's 70% gate, so the
  default 15-minute run would stop producing results. 5-minute regular bars
  (81%) and 1-minute regular bars (96%) clear it. The comparison is measured in
  ``tests/research/test_signal_study.py``.
* **Warmup.** None. Features run inside the window (the same one continuous
  series, straight across overnight gaps; their first values are NaN) and the
  engine needs at least ``SignalConfig.min_bars_for_signal`` bars.
* **Look-forward.** 15 bars, as above. Nothing is read past the window's end, so
  the last 15 bars of the window have no target (``window_runs_off_end``).
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (inclusive,
  canonical calendar), completed sessions only: a window ending on a session that
  has not finished is refused, where the provider used to hand back the forming
  session's partial bars. The 70/30 train/test split is by bar index. Regime
  labels and walk-forward months bucket by **UTC** date; both are unchanged.
* **Cache and result.** Before: Redis ``signal_engine``, keyed on ticker, feature,
  dates, size and the two switches, looked up before the fetch, 24 h, not shared
  across sources. Now: a new namespace keyed on the input identity, the admitted
  snapshot digest, the signal configuration and the calculation version. Looked
  up after admission (the digest needs the bytes), so a repaired or refreshed
  lake yields a new key, and an entry written when the provider was the source
  can never be found. Nothing is relabelled.

Differences a run can show against the provider path are documented and
measured in ``tests/research/test_signal_study_lake_vs_provider.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.jobs import cache as result_cache
from app.jobs.phases import friendly as friendly_phase
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.research.runs.hashing import hash_payload
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine
from app.research.study_inputs import (
    FOLD_RULE,
    AdjustmentMode,
    AdmittedMinuteInputs,
    MinuteWindowIdentity,
    SessionPolicy,
    StudyCoverage,
    StudyInputRefusal,
    admit_minute_inputs,
    bars_as_rows,
    fold_minute_bars,
)
from app.research.target import DEFAULT_NON_NAN_RATIO_THRESHOLD

logger = logging.getLogger(__name__)

#: Bump when the signal, backtest, walk-forward, diagnostics or graduation math changes: a cached result is the
#: product of the code that made it, and the cache key is the only thing that tells a new run apart from an old one.
SIGNAL_STUDY_CALCULATION_VERSION = "signal-study/1"

#: The lake-sourced cache namespace. Entries written when the provider was the source live under
#: ``signal_engine`` and are never read by a lake request.
CACHE_NAMESPACE = "signal_engine:lake:v1"

_REQUESTER = "signal-study"

_MINUTES_PER_DAY = 24 * 60
_TIMESPAN_MINUTES = {"minute": 1, "hour": 60, "day": _MINUTES_PER_DAY}


def max_target_share(bar_minutes: int, horizon_bars: int) -> float:
    """The most bars of one New York date that can have a ``horizon_bars``-ahead target, as a share of that date's bars.

    A target is valid only when the forward bar sits on the same date. A date holds at most ``n = ceil(1440 /
    bar_minutes)`` bars, and the last ``horizon_bars`` of them have no forward bar, so the share is ``(n - h) / n``
    at best. It is a ceiling for any data: gaps and a shorter session only lower it.

    Formula: share_max(m, h) = max(n - h, 0) / n, with n = ceil(1440 / m) bars of m minutes in a day
    Reference: Internal — the same-New-York-date rule of ``app/research/target.py::compute_forward_log_return``; no
      external source.
    Canonical implementation: this file.
    Validated against: tests/research/test_signal_study.py::test_the_ceiling_on_valid_targets_is_what_the_target_function_gives_on_gap_free_bars
    """
    per_day = -(-_MINUTES_PER_DAY // bar_minutes)
    return max(per_day - horizon_bars, 0) / per_day


#: The longest minute bar that can still reach the engine's validity gate (see :func:`max_target_share`).
MAX_BAR_MINUTES: int = max(
    minutes
    for minutes in range(1, _MINUTES_PER_DAY + 1)
    if max_target_share(minutes, SignalConfig().horizon) >= DEFAULT_NON_NAN_RATIO_THRESHOLD
)


@dataclass(frozen=True)
class SignalStudyParams:
    """The scientific request: what a run is asked to study."""

    symbol: str
    feature_name: str
    from_date: date
    to_date: date
    timespan: str
    multiplier: int
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    session_policy: SessionPolicy = "extended"
    price_adjustment_mode: AdjustmentMode = "polygon_split_adjusted"

    def config(self) -> SignalConfig:
        return SignalConfig(
            feature_name=self.feature_name, flip_sign=self.flip_sign, regime_gate_enabled=self.regime_gate_enabled
        )


@dataclass(frozen=True)
class SignalStudyIdentity:
    """Every input that decides this study's data: the lake input plus how the study shapes it."""

    inputs: MinuteWindowIdentity
    bar_minutes: int
    look_forward_bars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.inputs.as_dict(),
            "bar_minutes": self.bar_minutes,
            "aggregation": FOLD_RULE,
            "look_forward_bars": self.look_forward_bars,
            "look_forward_extends_window": False,
        }

    def digest(self) -> str:
        return hash_payload(self.as_dict())


def signal_study_identity(params: SignalStudyParams) -> SignalStudyIdentity:
    """The study's input identity, or a typed refusal for a resolution it can never run on."""
    horizon = SignalConfig().horizon
    unit = _TIMESPAN_MINUTES.get(params.timespan)
    if unit is None:
        raise StudyInputRefusal("resolution_unsupported", f"{params.timespan!r} is not a bar size this study reads.")
    bar_minutes = params.multiplier * unit
    ceiling = max_target_share(bar_minutes, horizon)
    if ceiling < DEFAULT_NON_NAN_RATIO_THRESHOLD:
        per_day = -(-_MINUTES_PER_DAY // bar_minutes)
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"{params.multiplier}-{params.timespan} bars are too long for the {horizon}-bar forward target: a day holds "
            f"at most {per_day} of them, so at most {ceiling * 100:.1f}% of bars can have a target inside the same "
            f"session, and the study needs {DEFAULT_NON_NAN_RATIO_THRESHOLD * 100:.0f}%. "
            f"Minute bars of up to {MAX_BAR_MINUTES} minutes can reach that share.",
        )
    return SignalStudyIdentity(
        inputs=MinuteWindowIdentity(
            symbol=params.symbol,
            price_adjustment_mode=params.price_adjustment_mode,
            session_policy=params.session_policy,
            window_start=params.from_date,
            window_end=params.to_date,
            warmup_sessions=0,
        ),
        bar_minutes=bar_minutes,
        look_forward_bars=horizon,
    )


def cache_params(identity: SignalStudyIdentity, admitted: AdmittedMinuteInputs, config: SignalConfig) -> dict[str, Any]:
    """The cache identity: data policy, the admitted bytes, the scientific inputs and the code that computes them."""
    return {
        "identity": identity.as_dict(),
        "data_root_id": str(admitted.data_root_id),
        "snapshot_digest": admitted.snapshot_digest,
        "signal_config": asdict(config),
        "calculation_version": SIGNAL_STUDY_CALCULATION_VERSION,
    }


def data_source_block(
    identity: SignalStudyIdentity | None,
    *,
    coverage: StudyCoverage | None,
    admitted: AdmittedMinuteInputs | None = None,
    bars_used: int | None = None,
    cache_hit: bool = False,
    cached_at_ms: int | None = None,
) -> dict[str, Any]:
    """The provenance a data-source line shows: the identity read, the admitted bytes, and the coverage counts."""
    block: dict[str, Any] = {"kind": "lake", **(identity.as_dict() if identity is not None else {})}
    block.update(
        identity_digest=identity.digest() if identity is not None else None,
        calculation_version=SIGNAL_STUDY_CALCULATION_VERSION,
        data_root_id=str(admitted.data_root_id) if admitted is not None else None,
        snapshot_digest=admitted.snapshot_digest if admitted is not None else None,
        corporate_action_versions=dict(admitted.snapshot.corporate_action_versions) if admitted is not None else {},
        coverage=coverage.as_dict() if coverage is not None else None,
        minute_bars_read=len(admitted.window_bars) if admitted is not None else None,
        bars_used=bars_used,
        cache={"hit": cache_hit, "cached_at_ms": cached_at_ms},
    )
    return block


def serialize_signal_report(report: Any) -> dict:
    """Serialize a SignalEngineReport for storage and delivery."""
    return {
        "success": report.error is None,
        "ticker": report.ticker,
        "feature_name": report.feature_name,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "bars_used": report.bars_used,
        "flip_sign": report.flip_sign,
        "thresholds_tested": report.thresholds_tested,
        "cost_bps_options": report.cost_bps_options,
        "best_threshold": report.best_threshold,
        "best_cost_bps": report.best_cost_bps,
        "backtest_grid": [asdict(bt) for bt in report.backtest_grid],
        "walk_forward": asdict(report.walk_forward) if report.walk_forward is not None else None,
        "graduation": asdict(report.graduation) if report.graduation is not None else None,
        "signal_diagnostics": (asdict(report.signal_diagnostics) if report.signal_diagnostics is not None else None),
        "data_sufficiency": (asdict(report.data_sufficiency) if report.data_sufficiency is not None else None),
        "effective_sample": (asdict(report.effective_sample) if report.effective_sample is not None else None),
        "regime_coverage": report.regime_coverage,
        "joint_regime_coverage": [asdict(b) for b in report.joint_regime_coverage],
        "signal_behavior": (asdict(report.signal_behavior) if report.signal_behavior is not None else None),
        "oos_sharpe_ci": (asdict(report.oos_sharpe_ci) if report.oos_sharpe_ci is not None else None),
        "deflated_sharpe": (asdict(report.deflated_sharpe) if report.deflated_sharpe is not None else None),
        "methodology": report.methodology,
        "research_log": report.research_log,
        "error": report.error,
    }


def _phase(emit: ProgressEmitter, cancel: CancellationCheck, phase_id: str, message: str | None = None) -> None:
    cancel.raise_if_cancelled()
    emit.phase(phase_id)
    emit.log(message if message is not None else friendly_phase("signal_engine", phase_id))


def _coverage_line(coverage: StudyCoverage) -> str:
    window = coverage.window
    return (
        f"Lake coverage: {window.expected} sessions, {window.reused} reused, {window.added} added, "
        f"{window.missing} missing, {window.failed} failed "
        f"(warmup: {coverage.warmup.expected} sessions; artifacts fetched {coverage.artifacts_fetched}, "
        f"reused {coverage.artifacts_reused})"
    )


def run_signal_study(
    params: SignalStudyParams,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None = None,
) -> dict | None:
    """Admit the study's lake inputs, compute, and return the result — or answer a refusal or cache hit itself.

    Returns the result dict for the job runner to complete. Returns ``None``
    when the job's terminal event is already emitted here: a refusal fails
    the job (typed code, plain reason) and parks the refusal payload with its
    counts as the job's result; a cache hit completes it flagged ``cached``.
    Blocking: runs on the job's worker thread.
    """
    identity: SignalStudyIdentity | None = None
    try:
        identity = signal_study_identity(params)
        return _run(params, identity, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=now_ms)
    except StudyInputRefusal as refusal:
        logger.warning(
            "signal study refused",
            extra={"action": "signal_study_refused", "code": refusal.code, "symbol": params.symbol},
        )
        emit.log(refusal.reason, level="warn")
        emit.failed(
            refusal.code,
            refusal.reason,
            result={
                "success": False,
                "status": "refused",
                "ticker": params.symbol,
                "feature_name": params.feature_name,
                "error": refusal.reason,
                "refusal": refusal.as_dict(),
                "data_source": data_source_block(identity, coverage=refusal.coverage),
            },
        )
        return None


def _run(
    params: SignalStudyParams,
    identity: SignalStudyIdentity,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None,
) -> dict | None:
    symbol = params.symbol
    config = params.config()
    _phase(
        emit,
        cancel,
        "admitting_data",
        f"Checking the lake for {symbol} {params.multiplier}-minute bars from "
        f"{params.from_date.isoformat()} to {params.to_date.isoformat()}",
    )
    admitted = admit_minute_inputs(
        identity.inputs, requester=_REQUESTER, now_ms=now_ms, check_cancelled=cancel.raise_if_cancelled
    )
    emit.log(_coverage_line(admitted.coverage))

    _phase(emit, cancel, "loading_bars", f"Loading {symbol} {params.multiplier}-minute bars from the lake")
    bars = bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes))
    emit.log(f"Loaded {len(bars):,} bars from {len(admitted.window_bars):,} minute bars")

    key = cache_params(identity, admitted, config)
    if not force:
        hit = result_cache.lookup(CACHE_NAMESPACE, key)
        if hit is not None:
            _, cached = hit
            cached_at = int(cached.get("cached_at", 0))
            result = {
                **cached["result"],
                "data_source": data_source_block(
                    identity,
                    coverage=admitted.coverage,
                    admitted=admitted,
                    bars_used=len(bars),
                    cache_hit=True,
                    cached_at_ms=cached_at,
                ),
            }
            result_cache.serve_cached_result(
                job_id, "signal_engine", {"result": result, "cached_at": cached_at}, announce_start=False
            )
            return None

    def on_phase(phase: str) -> None:
        cancel.raise_if_cancelled()
        emit.phase(phase)

    def on_log(message: str, level: str = "info") -> None:
        emit.log(message, level=level)

    def on_progress(current: int, total: int, unit: str = "configs", message: str | None = None) -> None:
        emit.progress(current=current, total=total, unit=unit, message=message)

    def cancel_check() -> bool:
        # Raises JobCancelled when set; otherwise returns False so call sites can use it either as a poll or as a
        # sync barrier.
        cancel.raise_if_cancelled()
        return False

    report = run_signal_engine(
        ticker=symbol,
        feature_name=params.feature_name,
        bars=bars,
        start_date=params.from_date.isoformat(),
        end_date=params.to_date.isoformat(),
        config=config,
        on_phase=on_phase,
        on_log=on_log,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    if report.error:
        # Surface the failure as an exception so run_in_thread emits job.failed (the engine caught the exception
        # internally to populate report.error; re-raising gives the SSE consumer a clean terminal event instead of
        # "completed but error_field=…").
        raise ValueError(report.error)

    cancel.raise_if_cancelled()
    _phase(emit, cancel, "completed")
    scientific = serialize_signal_report(report)
    result_cache.store(CACHE_NAMESPACE, key, scientific)
    return {
        **scientific,
        "data_source": data_source_block(
            identity, coverage=admitted.coverage, admitted=admitted, bars_used=len(bars)
        ),
    }
