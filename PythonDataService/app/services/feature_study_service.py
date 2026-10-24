"""Feature study (Research Lab → Features → Validate): computes from admitted lake inputs (#3521).

The study's input contract. What it read before #3521 (slice-1 census, #3491),
and what it reads now, field by field:

* **Bars.** Before: one Polygon ``fetch_aggregates`` call, trade aggregates for
  one stock symbol, in the size the request named. Now: the lake's one-minute
  trade bars, admitted through :mod:`app.research.study_inputs` (the existing
  materializer and committed-byte admission; the UI is never a writer), then
  folded to the requested size by ``fold_minute_bars`` — never the chart's
  resampler, which fills missing minutes forward.
* **Resolution.** Minute bars of 1, 3, 5 or 15 minutes. The target looks 15
  wall-clock minutes ahead, so the bar length must divide 15: hour, daily and
  the other minute multiples (2, 4, ×60, ×240, ...) failed the run every time
  and are now refused up front with a reason (#3527).
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only), never set
  explicitly. Now: the lake's ``polygon_split_adjusted`` mode, named in the
  identity. Prices sit on the lake's 1/10,000 grid (rounded half-up).
* **Session.** Before and now: **extended hours** — every bar the provider
  served, 04:00 to 20:00 ET. The browser sends ``session: "rth"`` and the
  backend never read it; that label stays inert so this migration does not
  silently change the sample. The session is now an explicit identity field
  (``session_policy``, default ``extended``); asking for ``regular`` is opt-in.
  Which session the study *should* use is an owner decision (D02).
* **Warmup.** None. Features use only bars inside the window (RSI-14, MACD
  12/26/9, rolling 30, ``pct_change(5)``), over one continuous series straight
  across overnight gaps; their first values in the window are NaN.
* **Look-forward.** 15 wall-clock minutes (``ResearchConfig.horizon_minutes``).
  A bar's target is NaN when the forward bar falls on another New York date or
  arrives outside 15 to 20 minutes. Nothing is read past the window's end, so
  the last bars of each session and of the window have no target.
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (inclusive,
  canonical calendar). Daily IC groups by UTC date while the target masks by
  New York date; both are unchanged.
* **Cache and result.** Before: Redis ``feature_research``, keyed on ticker,
  feature, dates and size, looked up before the fetch, 24 h, not shared across
  sources. Now: a new namespace keyed on the input identity, the admitted
  snapshot digest, the research configuration and the calculation version.
  Looked up after admission (the digest needs the bytes), so a repaired or
  refreshed lake yields a new key, and an entry written when the provider was
  the source can never be found. Nothing is relabelled.

Differences a run can show against the provider path are documented and
measured in ``tests/research/test_feature_study_lake_vs_provider.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.jobs import cache as result_cache
from app.jobs.phases import friendly as friendly_phase
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.research.config import ResearchConfig
from app.research.runner import ResearchReport, run_feature_research
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import (
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

logger = logging.getLogger(__name__)

#: Bump when the feature, target, IC, stationarity, quantile or robustness math changes: a cached result is the
#: product of the code that made it, and the cache key is the only thing that tells a new run apart from an old one.
FEATURE_STUDY_CALCULATION_VERSION = "feature-study/1"

#: The lake-sourced cache namespace. Entries written when the provider was the source live under
#: ``feature_research`` and are never read by a lake request.
CACHE_NAMESPACE = "feature_research:lake:v1"

#: The fold rule named in the identity: what a coarser bar is made of.
FOLD_RULE = "minute_fold_et_wall_clock_no_fill"

_REQUESTER = "feature-study"

#: Bar lengths, in minutes, that divide the look-forward target.
SUPPORTED_BAR_MINUTES: tuple[int, ...] = tuple(
    minutes
    for minutes in range(1, ResearchConfig().horizon_minutes + 1)
    if ResearchConfig().horizon_minutes % minutes == 0
)


@dataclass(frozen=True)
class FeatureStudyParams:
    """The scientific request: what a run is asked to study."""

    symbol: str
    feature_name: str
    from_date: date
    to_date: date
    timespan: str
    multiplier: int
    session_policy: SessionPolicy = "extended"
    price_adjustment_mode: AdjustmentMode = "polygon_split_adjusted"


@dataclass(frozen=True)
class FeatureStudyIdentity:
    """Every input that decides this study's numbers: the lake input plus how the study shapes it."""

    inputs: MinuteWindowIdentity
    bar_minutes: int
    look_forward_minutes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.inputs.as_dict(),
            "bar_minutes": self.bar_minutes,
            "aggregation": FOLD_RULE,
            "look_forward_minutes": self.look_forward_minutes,
            "look_forward_extends_window": False,
        }

    def digest(self) -> str:
        return hash_payload(self.as_dict())


def feature_study_identity(params: FeatureStudyParams) -> FeatureStudyIdentity:
    """The study's input identity, or a typed refusal for a resolution it can never run on."""
    look_forward = ResearchConfig().horizon_minutes
    if params.timespan != "minute":
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"{params.timespan} bars are longer than the {look_forward}-minute forward target, so the study cannot form "
            f"that target from them. Use minute bars of {_supported_list()} minutes.",
        )
    if params.multiplier not in SUPPORTED_BAR_MINUTES:
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"{params.multiplier}-minute bars do not divide the {look_forward}-minute forward target, so the study "
            f"cannot form it. Use minute bars of {_supported_list()} minutes.",
        )
    return FeatureStudyIdentity(
        inputs=MinuteWindowIdentity(
            symbol=params.symbol,
            price_adjustment_mode=params.price_adjustment_mode,
            session_policy=params.session_policy,
            window_start=params.from_date,
            window_end=params.to_date,
            warmup_sessions=0,
        ),
        bar_minutes=params.multiplier,
        look_forward_minutes=look_forward,
    )


def _supported_list() -> str:
    return ", ".join(str(minutes) for minutes in SUPPORTED_BAR_MINUTES)


def cache_params(identity: FeatureStudyIdentity, admitted: AdmittedMinuteInputs, feature_name: str) -> dict[str, Any]:
    """The cache identity: data policy, the admitted bytes, the scientific inputs and the code that computes them."""
    return {
        "identity": identity.as_dict(),
        "data_root_id": str(admitted.data_root_id),
        "snapshot_digest": admitted.snapshot_digest,
        "feature_name": feature_name,
        "research_config": asdict(ResearchConfig()),
        "calculation_version": FEATURE_STUDY_CALCULATION_VERSION,
    }


def data_source_block(
    identity: FeatureStudyIdentity | None,
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
        calculation_version=FEATURE_STUDY_CALCULATION_VERSION,
        data_root_id=str(admitted.data_root_id) if admitted is not None else None,
        snapshot_digest=admitted.snapshot_digest if admitted is not None else None,
        corporate_action_versions=dict(admitted.snapshot.corporate_action_versions) if admitted is not None else {},
        coverage=coverage.as_dict() if coverage is not None else None,
        minute_bars_read=len(admitted.window_bars) if admitted is not None else None,
        bars_used=bars_used,
        cache={"hit": cache_hit, "cached_at_ms": cached_at_ms},
    )
    return block


def _serialize_target(target: Any) -> dict:
    """Project a ``TargetResult`` to the JSON-friendly ``TargetMetadata`` shape the Frontend consumes.

    The bulky ``values``/``timestamps`` Series are intentionally dropped — only the metadata that drives the UI
    disclosure travels with the report. ``invalid_reason_counts`` is emitted as a list of ``{reason, count}``.
    """
    return {
        "target_name": target.target_name,
        "horizon_minutes": target.horizon_minutes,
        "horizon_bars": target.horizon_bars,
        "bar_minutes": target.bar_minutes,
        "timezone": target.timezone,
        "valid_count": target.valid_count,
        "total_count": target.total_count,
        "valid_ratio": target.valid_ratio,
        "invalid_reason_counts": [
            {"reason": reason, "count": count} for reason, count in target.invalid_reason_counts.items()
        ],
    }


def serialize_feature_report(report: ResearchReport) -> dict:
    """Serialize a ResearchReport for storage in the result cache and delivery to the Frontend.

    Hand-rolled (rather than ``asdict``) so we can keep the Pydantic-response-model shape stable: nested
    dataclasses become dicts and we flatten the validation verdict.
    """
    return {
        "success": report.error is None,
        "ticker": report.ticker,
        "feature_name": report.feature_name,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "bars_used": report.bars_used,
        "mean_ic": report.mean_ic,
        "ic_t_stat": report.ic_t_stat,
        "ic_p_value": report.ic_p_value,
        "nw_t_stat": report.nw_t_stat,
        "nw_p_value": report.nw_p_value,
        "effective_n": report.effective_n,
        "ic_values": report.ic_values,
        "ic_dates": report.ic_dates,
        "adf_pvalue": report.adf_pvalue,
        "kpss_pvalue": report.kpss_pvalue,
        "is_stationary": report.is_stationary,
        "quantile_bins": report.quantile_bins,
        "is_monotonic": report.is_monotonic,
        "monotonicity_ratio": report.monotonicity_ratio,
        "robustness": asdict(report.robustness) if report.robustness is not None else None,
        "feature_spec": asdict(report.feature_spec) if report.feature_spec is not None else None,
        "validation_verdict": (asdict(report.validation_verdict) if report.validation_verdict is not None else None),
        "target": _serialize_target(report.target) if report.target is not None else None,
        "passed_validation": report.passed_validation,
        "error": report.error,
    }


def _phase(emit: ProgressEmitter, cancel: CancellationCheck, phase_id: str, message: str | None = None) -> None:
    cancel.raise_if_cancelled()
    emit.phase(phase_id)
    emit.log(message if message is not None else friendly_phase("feature_research", phase_id))


def _coverage_line(coverage: StudyCoverage) -> str:
    window = coverage.window
    return (
        f"Lake coverage: {window.expected} sessions, {window.reused} reused, {window.added} added, "
        f"{window.missing} missing, {window.failed} failed "
        f"(warmup: {coverage.warmup.expected} sessions; artifacts fetched {coverage.artifacts_fetched}, "
        f"reused {coverage.artifacts_reused})"
    )


def run_feature_study(
    params: FeatureStudyParams,
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
    identity: FeatureStudyIdentity | None = None
    try:
        identity = feature_study_identity(params)
        return _run(params, identity, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=now_ms)
    except StudyInputRefusal as refusal:
        logger.warning(
            "feature study refused",
            extra={"action": "feature_study_refused", "code": refusal.code, "symbol": params.symbol},
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
    params: FeatureStudyParams,
    identity: FeatureStudyIdentity,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None,
) -> dict | None:
    symbol = params.symbol
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
    emit.progress(current=len(bars), total=len(bars), unit="bars", message="bars loaded")

    key = cache_params(identity, admitted, params.feature_name)
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
                job_id, "feature_research", {"result": result, "cached_at": cached_at}, announce_start=False
            )
            return None

    # The runner emits its own phase events via callbacks; we forward them through the friendly-label table so the
    # UI text reads cleanly without the runner having to know about phase ids.
    def on_phase(phase: str) -> None:
        cancel.raise_if_cancelled()
        emit.phase(phase)

    def on_log(message: str, level: str = "info") -> None:
        emit.log(message, level=level)

    def on_progress(current: int, total: int, unit: str = "windows", message: str | None = None) -> None:
        emit.progress(current=current, total=total, unit=unit, message=message)

    def cancel_check() -> bool:
        # Raises JobCancelled when set; otherwise returns False so call sites can use it either as a poll or as a
        # sync barrier.
        cancel.raise_if_cancelled()
        return False

    report = run_feature_research(
        ticker=symbol,
        feature_name=params.feature_name,
        bars=bars,
        start_date=params.from_date.isoformat(),
        end_date=params.to_date.isoformat(),
        config=ResearchConfig(),
        on_phase=on_phase,
        on_log=on_log,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    if report.error:
        # Surface the failure as an exception so run_in_thread emits job.failed (the runner caught the exception
        # internally to populate report.error; here we re-raise so the SSE consumer sees a clean terminal event
        # instead of "completed but error_field=…").
        raise ValueError(report.error)

    cancel.raise_if_cancelled()
    _phase(emit, cancel, "completed")
    scientific = serialize_feature_report(report)
    result_cache.store(CACHE_NAMESPACE, key, scientific)
    return {
        **scientific,
        "data_source": data_source_block(
            identity, coverage=admitted.coverage, admitted=admitted, bars_used=len(bars)
        ),
    }
