"""Feature study (Research Lab → Features → Validate): computes from admitted lake inputs (#3521).

The study's input contract. What it read before #3521 (the data-source census, #3491),
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
  identity. Prices sit on the lake's 1/10,000 grid (rounded half-up): a change
  to the input data, not float rounding, whose effect on RSI and the mean IC
  grows as the price falls (measured at $150, $12 and $2 in the comparison test).
* **Session.** Before: **extended hours** — every bar the provider served,
  04:00 to 20:00 ET, whatever the request said (the old route never read the
  field the browser sent). Now the request's ``session`` is the one control and
  the person's choice (#3521): ``extended`` is the default, so no caller's
  sample moves, and ``rth`` is the scheduled session, named ``regular`` in the
  identity (``session_policy``). The two are different samples with different
  cache identities, and the page offers both. A session counts as complete only
  once the vendor has delivered its whole tape (the close, plus four hours of
  after-hours for ``extended``, plus the vendor's 15-minute delay), and a held
  day captured before that is refused (``captured_before_final``) rather than
  read as complete or captured again (``app.data_lake.capture_finality``). A
  refused day stays refused: there is no re-capture path (``docs/known-gaps.md``).
  The lake keeps one copy of a day for every reader, so a ``rth`` run that would
  have to capture a day waits for the extended tape too (``window_not_complete``).
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

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.research.config import ResearchConfig
from app.research.lake_study import FoldedStudyIdentity, LakeStudy, StudyRun, run_lake_study
from app.research.runner import ResearchReport, run_feature_research
from app.research.study_inputs import AdjustmentMode, MinuteWindowIdentity, SessionPolicy, StudyInputRefusal

#: Bump when the feature, target, IC, stationarity, quantile or robustness math changes: a cached result is the
#: product of the code that made it, and the cache key is the only thing that tells a new run apart from an old one.
FEATURE_STUDY_CALCULATION_VERSION = "feature-study/1"

#: The lake-sourced cache namespace. Entries written when the provider was the source live under
#: ``feature_research`` and are never read by a lake request.
CACHE_NAMESPACE = "feature_research:lake:v1"

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
class FeatureStudyIdentity(FoldedStudyIdentity):
    """Every input that decides this study's numbers: the lake input, the bar length and the forward target."""

    look_forward_minutes: int

    def study_fields(self) -> dict[str, Any]:
        return {"look_forward_minutes": self.look_forward_minutes, "look_forward_extends_window": False}


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


def feature_study(params: FeatureStudyParams) -> LakeStudy:
    """The Feature study as the shared lake run takes it: its names, its cache inputs, and its math."""
    config = ResearchConfig()

    def compute(run: StudyRun) -> dict[str, Any]:
        report = run_feature_research(
            ticker=params.symbol,
            feature_name=params.feature_name,
            bars=run.rows,
            start_date=params.from_date.isoformat(),
            end_date=params.to_date.isoformat(),
            config=config,
            on_phase=run.hooks.on_phase,
            on_log=run.hooks.on_log,
            on_progress=run.hooks.on_progress,
            cancel_check=run.hooks.cancel_check,
        )
        if report.error:
            # Surface the failure as an exception so run_in_thread emits job.failed (the runner caught the exception
            # internally to populate report.error; here we re-raise so the SSE consumer sees a clean terminal event
            # instead of "completed but error_field=…").
            raise ValueError(report.error)
        return serialize_feature_report(report)

    return LakeStudy(
        job_type="feature_research",
        cache_namespace=CACHE_NAMESPACE,
        calculation_version=FEATURE_STUDY_CALCULATION_VERSION,
        requester=_REQUESTER,
        subject={"ticker": params.symbol, "feature_name": params.feature_name},
        admit_message=(
            f"Checking the lake for {params.symbol} {params.multiplier}-minute bars from "
            f"{params.from_date.isoformat()} to {params.to_date.isoformat()}"
        ),
        load_message=f"Loading {params.symbol} {params.multiplier}-minute bars from the lake",
        compute=compute,
        cache_extra={"feature_name": params.feature_name, "research_config": asdict(config)},
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

    The run is the shared one (:func:`app.research.lake_study.run_lake_study`); this supplies the identity and the math.
    """
    return run_lake_study(
        feature_study(params),
        lambda: feature_study_identity(params),
        job_id=job_id,
        force=force,
        emit=emit,
        cancel=cancel,
        now_ms=now_ms,
    )
