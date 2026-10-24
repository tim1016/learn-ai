"""Indicator reliability study (Research Lab → Backtests → Reliability): admits its lake inputs before it computes (#3523).

The study's input contract. What it read before #3523 (the data-source census, #3491),
and what it reads now, field by field:

* **Bars.** Before: ``fetch_bars_chunked`` — Polygon aggregates in date chunks that
  fail fast on a duplicate or out-of-order bar — from the warmup start through
  ``to_date``, in the bar size the request named. Now: the lake's one-minute trade
  bars, admitted through :mod:`app.research.study_inputs` (the existing materializer
  and committed-byte admission; the UI is never a writer), folded to the requested
  size by ``fold_minute_bars`` — never the chart's resampler, which fills missing
  minutes forward.
* **Resolution.** The API accepts minute, hour and day; the browser always sends
  one-minute bars (its Sampling card offers no other size). Now: minute bars whose length divides
  the hour (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60) and one hour, so every bar
  starts on a wall-clock boundary. Anything else is refused up front with a reason.
  The study reads **minute artifacts only**: it neither reads nor waits on the lake's
  derived daily rollup, which folds extended-hours trades and is not the vendor's daily
  bar, so a daily request is refused instead of answered from it.
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only), set explicitly. Now:
  the lake's ``polygon_split_adjusted`` mode, named in the identity. Prices sit on the
  lake's 1/10,000 grid (rounded half-up), and carry the corporate actions known at the
  lake's latest capture of the symbol rather than at request time.
* **Session.** Regular hours before #3523, and the default now (the owner's choice on #3523: no
  number moves unless the person picks the other session). The request's ``session`` is the one control:
  ``rth`` reads the scheduled session, named ``regular`` in the identity (``session_policy``), and ``extended``
  reads every bar the provider served, pre-market and after-hours included. They are different samples with
  different identities. Before: ``filter_session(df, "rth")`` after the fetch, called without the ``timespan`` and
  ``multiplier`` of the bars (#3527), so an hour request lost the bar holding the open and a daily request lost
  every bar; the request's ``session`` was ignored. Now the lake reader's session — canonical calendar windows,
  early closes respected — is applied to the minute bars *before* the fold, so a bar of any length holds exactly the
  session's minutes inside it. Under ``rth`` a bar that straddles the open (lengths that do not divide 30 minutes: 4,
  12, 20, 60) holds only the minutes from 09:30; the vendor's aggregate for the same bucket also held the pre-open
  trades. Under ``extended`` the rule does not apply: the bar holds every minute the lake has in it. A session counts
  as complete only once the vendor has delivered its whole tape for the session read (the close, plus four hours of
  after-hours for ``extended``, plus the vendor's 15-minute delay), and a held day captured before that is refused
  (``captured_before_final``) rather than read as complete (``app.data_lake.capture_finality``). The lake keeps one
  copy of a day for every reader, so an ``rth`` run that would have to capture a day waits for the extended tape too
  (``window_not_complete``).
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (canonical calendar).
  A window that ends on a session that has not finished is refused: the lake holds
  completed sessions only, where the provider path computed on the forming session's
  delayed bars.
* **Warmup.** Sized by ``dataset_service.resolve_indicator_window``, unchanged: the
  indicator's lookback times the warmup multiplier, in scheduled sessions, never before
  the provider's history floor. Before: a lead-in cut short by the floor, the symbol's
  listing date, a gap or sparse sessions was computed on without a word. Now: the
  sessions the sizing names are admitted with the window (counted apart as warmup) and
  the result says how many bars the lead-in really held (:class:`ReliabilityWarmup`).
  The oldest lead-in sessions the vendor answered with no bars (``provider_no_data``:
  the symbol had not started trading) are dropped by admission itself
  (``lead_in="trim_unsupplied_prefix"``) and stay in the warmup counts. Any other
  vendor failure on a lead-in session — a rate limit, an API error, a tape the lake's
  publication gate rejected — refuses, as does a hole after the first admitted session.
  ``warmup_policy`` decides what a shortfall may do. ``allow_shortened`` (the default,
  what the study always did, now reported) computes on the lead-in that exists.
  ``require_full`` refuses any shortfall with ``warmup_shortened``. The sizing counts the bars a
  session is guaranteed to hold in regular hours, so an ``extended`` run reads the same sessions
  and gets at least as many lead-in bars.
* **Look-forward.** Horizons of 1 to 100 bars (default 1, 5, 10, 15, 30). A forward
  return that crosses a New York date is masked, nothing is read past ``to_date`` so the last
  bars of the window have no label, and the IC decay curve runs to the largest horizon
  plus ten. The mask read the UTC date before ``extended`` was offered; for regular hours the
  two dates are the same, so no ``rth`` number moved, and under ``extended`` in standard time
  the 19:00 to 20:00 ET bars (00:00 to 01:00 UTC) are no longer given a return that reaches
  into the next pre-market. The daily IC (:mod:`app.research.validation.ic`, shared with the
  Feature study) and the train and test dates group by the same New York date, so a session's
  evening counts with its own day and no day falls after ``to_date``.
* **Annualizing.** The IR and Sharpe proxies scale by the bars a trading year holds. Regular hours:
  390 one-minute bars a day, as before. Extended: the 04:00 to 20:00 ET span, 960 one-minute
  bars a day, counted by the scheduled span and not by the minutes that traded (as the regular
  figure is).
* **Guards.** "Insufficient data" (fewer bars than lookback plus the largest horizon,
  and fewer than 100 bars after the warmup is trimmed) are unchanged except that the
  first now counts the bars of the session the study reads, where it counted every bar
  the provider served.
* **Cache and result.** None before, none now: the study is synchronous and has never
  cached, and its random baseline is an unseeded Monte Carlo draw, so a stored result
  would freeze a different number than a rerun gives. What the response carries instead
  is the identity a cache or a saved result would key on (:func:`result_identity`): the
  input identity, the admitted snapshot's digest, the data root and the calculation
  version, under :data:`RESULT_NAMESPACE`. A repaired lake gives the same request a new
  snapshot digest; a changed policy gives it a new identity digest.

* **Span.** :func:`reliability_span` names the sessions a run asks the lake for (the warmup lead-in
  and the window) from the calendar and the warmup sizing alone, reading no bar, so the page can count
  how many the lake lacks before the run. The lead-in is sized here, in one place, and the browser derives
  none of it.

The shared lake-study pieces (:mod:`app.research.lake_study`) this study uses: the
identity (:class:`~app.research.lake_study.FoldedStudyIdentity`), the ``data_source``
block, the cache key its result identity is built from, and the refused-result body.
The one it does not: :func:`~app.research.lake_study.run_lake_study`, which is a job
runner — phases, progress events, cancellation, the result cache and the job's terminal
events. This study is one synchronous request with none of those, so it admits, folds and
hands the router a frame, and the router computes.

Differences a run can show against the provider path are documented and measured in
``tests/research/test_indicator_reliability_lake_vs_provider.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Literal

import pandas as pd

from app.lean_sidecar.trading_calendar import expected_sessions
from app.models.indicator_reliability_models import (
    ReliabilitySpan,
    ReliabilityWarmup,
    WarmupPolicy,
    WarmupShortfallCause,
)
from app.research.indicator_reliability import DEFAULT_VOL_WINDOW, MAX_DECAY_HORIZON, RANDOM_SIMULATIONS, TRAIN_RATIO
from app.research.lake_study import FoldedStudyIdentity, cache_key, data_source_block
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import (
    AdmittedMinuteInputs,
    MinuteWindowIdentity,
    RefusalCode,
    SessionPolicy,
    StudyCoverage,
    StudyInputRefusal,
    admit_minute_inputs,
    bars_as_rows,
    fold_minute_bars,
)
from app.services.dataset_service import (
    INDICATOR_CONFIGS,
    IndicatorWindow,
    bar_minutes_for,
    resolve_indicator_window,
)
from app.services.indicator_warmup_policy import requested_indicator_warmup_lookback
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Bump when the indicator series (``calculate_dynamic_indicators``, pandas-ta, ``INDICATOR_CONFIGS``) or the IC,
#: correction, baseline, regime or IR-proxy math changes: a result is the product of the code that made it, and this
#: version is what tells a result of the new math apart from one of the old.
RELIABILITY_CALCULATION_VERSION = "indicator-reliability/1"

#: The namespace a result cache or saved result for lake-sourced reliability studies would live under. Nothing is
#: stored in it today; the identity digests are computed under it so a later cache cannot collide with another study's.
RESULT_NAMESPACE = "indicator_reliability:lake:v1"

_REQUESTER = "indicator-reliability"

#: Minute bar lengths that start every bar on a wall-clock boundary (they divide the hour).
SUPPORTED_MINUTE_MULTIPLIERS: tuple[int, ...] = tuple(m for m in range(1, 61) if 60 % m == 0)

_FRAME_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Request, identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReliabilityParams:
    """The scientific request: what a run is asked to study."""

    symbol: str
    indicator_name: str
    indicator_params: Mapping[str, int | float]
    from_date: date
    to_date: date
    timespan: str
    multiplier: int
    horizons: tuple[int, ...]
    include_slope: bool = False
    warmup_policy: WarmupPolicy = "allow_shortened"
    #: ``regular`` is the scheduled session, the one the study has always computed on; ``extended`` is every bar served.
    session_policy: SessionPolicy = "regular"
    price_adjustment_mode: Literal["raw", "polygon_split_adjusted"] = "polygon_split_adjusted"


@dataclass(frozen=True)
class ReliabilityIdentity(FoldedStudyIdentity):
    """Every input that decides this study's numbers: the folded lake input plus how the study shapes it."""

    timespan: str
    multiplier: int
    indicator_name: str
    indicator_params: tuple[tuple[str, int | float], ...]
    horizons: tuple[int, ...]
    include_slope: bool

    def study_fields(self) -> dict[str, Any]:
        return {
            "timespan": self.timespan,
            "multiplier": self.multiplier,
            "indicator": {"name": self.indicator_name, "params": dict(self.indicator_params)},
            "horizons": list(self.horizons),
            "include_slope": self.include_slope,
            "study": {
                "train_ratio": TRAIN_RATIO,
                "random_simulations": RANDOM_SIMULATIONS,
                "vol_window": DEFAULT_VOL_WINDOW,
                "max_decay_horizon": MAX_DECAY_HORIZON,
            },
        }


# ---------------------------------------------------------------------------
# Refusal, warmup
# ---------------------------------------------------------------------------


class ReliabilityRefusal(StudyInputRefusal):
    """No result: the study's required input was not admitted, or the request names an input it cannot read.

    A :class:`StudyInputRefusal` that also carries the ``data_source`` block it was refused against (``None`` when
    nothing was read), so the response can show the counts beside the reason.
    """

    def __init__(
        self,
        code: RefusalCode,
        reason: str,
        *,
        coverage: StudyCoverage | None = None,
        data_source: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, reason, coverage=coverage)
        self.data_source = data_source


def study_bar_minutes(timespan: str, multiplier: int) -> int:
    """The study's bar length in minutes, or a typed refusal for a size the lake cannot form exactly."""
    if timespan == "minute" and multiplier in SUPPORTED_MINUTE_MULTIPLIERS:
        return multiplier
    if timespan == "hour" and multiplier == 1:
        return 60
    supported = ", ".join(str(m) for m in SUPPORTED_MINUTE_MULTIPLIERS)
    if timespan == "day":
        reason = (
            "Daily bars are not read from the lake: its daily rollup folds extended-hours trades and is not the "
            f"vendor's daily bar. Use minute bars of {supported} minutes, or one hour."
        )
    else:
        reason = (
            f"{multiplier} x {timespan} bars cannot be formed from the lake's one-minute bars on wall-clock "
            f"boundaries. Use minute bars of {supported} minutes, or one hour."
        )
    raise ReliabilityRefusal("resolution_unsupported", reason)


_CAUSE_WORDS: dict[WarmupShortfallCause, str] = {
    "none": "no shortfall",
    "provider_history_floor": "the provider's history does not reach back further",
    "symbol_history_starts": "the symbol has no bars before its first listed session",
    "sparse_session_bars": "the lead-in sessions hold fewer bars than a fully traded session",
}

#: What `require_full` tells the caller to change, by cause: starting later cannot add bars to a lead-in that is sparse.
_REMEDY: dict[WarmupShortfallCause, str] = {
    "none": "",
    "provider_history_floor": "Start the window later, or allow a shortened warmup.",
    "symbol_history_starts": "Start the window later, or allow a shortened warmup.",
    "sparse_session_bars": "Allow a shortened warmup to run on the bars it has.",
}


def warmup_report(
    policy: WarmupPolicy,
    *,
    requested_bars: int,
    requested_sessions: int,
    admitted_sessions: int,
    admitted_bars: int,
    dropped_sessions: int,
    floor_limited: bool,
) -> ReliabilityWarmup:
    """The lead-in the sizing asked for against the one admitted, and why it fell short, first match winning.

    ``dropped_sessions`` are the oldest lead-in sessions the vendor answered with no bars (the symbol had not started
    trading); ``floor_limited`` is the sizing's own answer that the provider's history floor ended the lead-in; a
    shortfall with neither is a lead-in whose every session was admitted but held fewer bars than a fully traded one.
    """
    shortfall = max(requested_bars - admitted_bars, 0)
    cause: WarmupShortfallCause
    if shortfall == 0:
        cause = "none"
    elif dropped_sessions:
        cause = "symbol_history_starts"
    elif floor_limited:
        cause = "provider_history_floor"
    else:
        cause = "sparse_session_bars"
    return ReliabilityWarmup(
        policy=policy,
        requested_bars=requested_bars,
        requested_sessions=requested_sessions,
        admitted_sessions=admitted_sessions,
        admitted_bars=admitted_bars,
        shortened=shortfall > 0,
        shortfall_bars=shortfall,
        cause=cause,
        notice=(
            None
            if shortfall == 0
            else f"Warmup is {shortfall} bars short of the {requested_bars} the sizing asks for "
            f"({_CAUSE_WORDS[cause]}); the indicator's first values may be less settled."
        ),
    )


# ---------------------------------------------------------------------------
# Result identity and provenance
# ---------------------------------------------------------------------------


def result_identity(identity: ReliabilityIdentity, admitted: AdmittedMinuteInputs) -> dict[str, Any]:
    """What a stored result of this study is a result *of*: data policy, admitted bytes, inputs and the code."""
    return {
        "namespace": RESULT_NAMESPACE,
        **cache_key(identity, admitted, calculation_version=RELIABILITY_CALCULATION_VERSION, extra={}),
    }


def reliability_data_source(
    identity: ReliabilityIdentity,
    *,
    warmup: ReliabilityWarmup | None,
    coverage: StudyCoverage | None,
    admitted: AdmittedMinuteInputs | None = None,
    bars_used: int | None = None,
) -> dict[str, Any]:
    """The shared ``data_source`` block, plus the warmup report and the identity a stored result would key on."""
    block = data_source_block(
        identity,
        calculation_version=RELIABILITY_CALCULATION_VERSION,
        coverage=coverage,
        admitted=admitted,
        bars_used=bars_used,
    )
    if block is None:
        raise ValueError("a reliability run always has an identity to name its source by")
    return {
        **block,
        "warmup": warmup.model_dump() if warmup is not None else None,
        "result_identity_digest": hash_payload(result_identity(identity, admitted)) if admitted is not None else None,
    }


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class AdmittedReliabilityInputs:
    """The study's bars — warmup lead-in and window, the session it reads, at its resolution — with their receipt.

    ``frame`` holds ``timestamp`` (bar start, ``int64 ms UTC``), ``open``, ``high``, ``low``, ``close`` and
    ``volume``. Rows before ``window_start_ms`` (ET midnight of ``from_date``) are the warmup lead-in: the
    indicator runs over them and the caller trims them.
    """

    identity: ReliabilityIdentity
    frame: pd.DataFrame
    window_start_ms: int
    max_lookback: int
    warmup: ReliabilityWarmup
    admitted: AdmittedMinuteInputs

    def data_source(self, *, bars_used: int | None = None) -> dict[str, Any]:
        return reliability_data_source(
            self.identity,
            warmup=self.warmup,
            coverage=self.admitted.coverage,
            admitted=self.admitted,
            bars_used=bars_used,
        )


def _lead_in_aware(refusal: StudyInputRefusal, from_date: date) -> str:
    """Say that the sessions that stop the run are the warmup's when every one of them is."""
    coverage = refusal.coverage
    if coverage is None or not coverage.blocking or any(issue.group != "warmup" for issue in coverage.blocking):
        return refusal.reason
    return f"{refusal.reason} Every failed session lies in the warmup lead-in before {from_date.isoformat()}."


@dataclass(frozen=True)
class WarmupSizing:
    """What the warmup sizing asks for: the bar length, the indicator's lookback, the window and the lead-in sessions."""

    bar_minutes: int
    max_lookback: int
    window: IndicatorWindow
    lead_in: list[date]


def size_warmup(
    *,
    indicator_name: str,
    indicator_params: Mapping[str, int | float],
    from_date: date,
    timespan: str,
    multiplier: int,
    now_ms: int,
) -> WarmupSizing:
    """Size the lead-in the run asks for, or refuse a bar size the lake cannot form. Calendar only: reads no bar."""
    bar_minutes = study_bar_minutes(timespan, multiplier)
    indicator_entry = {"name": indicator_name, "params": dict(indicator_params)}
    max_lookback = requested_indicator_warmup_lookback([indicator_entry], INDICATOR_CONFIGS)
    window = resolve_indicator_window(
        from_date.isoformat(),
        max_lookback=max_lookback,
        bar_minutes=bar_minutes_for(timespan, multiplier),
        now_ms=now_ms,
    )
    fetch_from = date.fromisoformat(window.fetch_from)
    lead = expected_sessions(fetch_from, from_date - timedelta(days=1)) if fetch_from < from_date else []
    return WarmupSizing(bar_minutes=bar_minutes, max_lookback=max_lookback, window=window, lead_in=lead)


def reliability_span(
    *,
    indicator_name: str,
    indicator_params: Mapping[str, int | float],
    from_date: date,
    to_date: date,
    timespan: str,
    multiplier: int,
    now_ms: int | None = None,
) -> ReliabilitySpan:
    """The span of scheduled sessions a run of this request asks the lake for: its lead-in and its window.

    The same sizing :func:`admit_reliability_inputs` runs, so the page's count and the run cannot disagree about the
    lead-in. Raises :class:`ReliabilityRefusal` for a bar size the lake cannot form.
    """
    sizing = size_warmup(
        indicator_name=indicator_name,
        indicator_params=indicator_params,
        from_date=from_date,
        timespan=timespan,
        multiplier=multiplier,
        now_ms=now_ms_utc() if now_ms is None else now_ms,
    )
    return ReliabilitySpan(
        start_ms=et_midnight_ms(sizing.lead_in[0] if sizing.lead_in else from_date),
        end_ms=et_midnight_ms(to_date),
        warmup_sessions=len(sizing.lead_in),
    )


def admit_reliability_inputs(params: ReliabilityParams, *, now_ms: int | None = None) -> AdmittedReliabilityInputs:
    """Size the warmup, admit the lake inputs for it and the window, and hand over the study's bars — or refuse.

    Blocking: call from a worker thread (materialization and byte admission cross to the shared background loop).
    Raises :class:`ReliabilityRefusal`; nothing is computed on a series that admission did not complete, and a
    shortfall in the lead-in is either reported or refused, never silent.
    """
    now = now_ms_utc() if now_ms is None else now_ms
    sizing = size_warmup(
        indicator_name=params.indicator_name,
        indicator_params=params.indicator_params,
        from_date=params.from_date,
        timespan=params.timespan,
        multiplier=params.multiplier,
        now_ms=now,
    )
    bar_minutes, max_lookback, window, lead = sizing.bar_minutes, sizing.max_lookback, sizing.window, sizing.lead_in

    requested_inputs = MinuteWindowIdentity(
        symbol=params.symbol,
        price_adjustment_mode=params.price_adjustment_mode,
        session_policy=params.session_policy,
        window_start=params.from_date,
        window_end=params.to_date,
        warmup_sessions=len(lead),
    )
    requested = ReliabilityIdentity(
        inputs=requested_inputs,
        bar_minutes=bar_minutes,
        timespan=params.timespan,
        multiplier=params.multiplier,
        indicator_name=params.indicator_name,
        indicator_params=tuple(sorted(params.indicator_params.items())),
        horizons=params.horizons,
        include_slope=params.include_slope,
    )
    try:
        admitted = admit_minute_inputs(
            requested_inputs, requester=_REQUESTER, now_ms=now, lead_in="trim_unsupplied_prefix"
        )
    except StudyInputRefusal as refusal:
        raise ReliabilityRefusal(
            refusal.code,
            _lead_in_aware(refusal, params.from_date),
            coverage=refusal.coverage,
            data_source=reliability_data_source(requested, warmup=None, coverage=refusal.coverage),
        ) from refusal

    identity = replace(requested, inputs=admitted.identity)
    frame = pd.DataFrame(
        bars_as_rows(fold_minute_bars((*admitted.warmup_bars, *admitted.window_bars), bar_minutes)),
        columns=_FRAME_COLUMNS,
    )
    report = warmup_report(
        params.warmup_policy,
        requested_bars=window.warmup_bars,
        requested_sessions=len(lead),
        admitted_sessions=admitted.identity.warmup_sessions,
        admitted_bars=int((frame["timestamp"] < window.window_start_ms).sum()),
        dropped_sessions=len(admitted.coverage.dropped),
        floor_limited=window.floor_limited,
    )
    if report.shortened:
        logger.info(
            "reliability warmup shorter than the sizing asks",
            extra={
                "action": "reliability_warmup_shortened",
                "symbol": params.symbol,
                "cause": report.cause,
                "requested_bars": report.requested_bars,
                "admitted_bars": report.admitted_bars,
                "policy": report.policy,
            },
        )
    if report.shortened and params.warmup_policy == "require_full":
        raise ReliabilityRefusal(
            "warmup_shortened",
            f"The warmup asks for {report.requested_bars} bars before {params.from_date.isoformat()} and the lake holds "
            f"{report.admitted_bars} ({_CAUSE_WORDS[report.cause]}). {_REMEDY[report.cause]}",
            coverage=admitted.coverage,
            data_source=reliability_data_source(identity, warmup=report, coverage=admitted.coverage, admitted=admitted),
        )
    return AdmittedReliabilityInputs(
        identity=identity,
        frame=frame,
        window_start_ms=window.window_start_ms,
        max_lookback=max_lookback,
        warmup=report,
        admitted=admitted,
    )
