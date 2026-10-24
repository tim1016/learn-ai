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
  one-minute bars (its Sampling card is hidden). Now: minute bars whose length divides
  the hour (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60) and one hour, so every bar
  starts on a wall-clock boundary. Anything else is refused up front with a reason.
  The study reads **minute artifacts only**: it neither reads nor waits on the lake's
  derived daily rollup, which folds extended-hours trades and is not the vendor's daily
  bar, so a daily request is refused instead of answered from it.
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only), set explicitly. Now:
  the lake's ``polygon_split_adjusted`` mode, named in the identity. Prices sit on the
  lake's 1/10,000 grid (rounded half-up), and carry the corporate actions known at the
  lake's latest capture of the symbol rather than at request time.
* **Session.** Regular hours, before and now. Before: ``filter_session(df, "rth")``
  after the fetch, called without the ``timespan`` and ``multiplier`` of the bars
  (#3527), so an hour request lost the bar holding the open and a daily request lost
  every bar. Now: the lake reader's regular session — canonical calendar windows, early
  closes respected — is applied to the minute bars *before* the fold, so a bar of any
  length holds exactly the regular-hours minutes inside it. A bar that straddles the
  open (lengths that do not divide 30 minutes: 4, 12, 20, 60) holds only those minutes;
  the vendor's aggregate for the same bucket also held the pre-open trades. The
  request's ``session`` field was ignored and stays ignored.
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (canonical calendar).
  A window that ends on a session that has not finished is refused: the lake holds
  completed sessions only, where the provider path computed on the forming session's
  delayed bars.
* **Warmup.** Sized by ``dataset_service.resolve_indicator_window``, unchanged: the
  indicator's lookback times the warmup multiplier, in scheduled sessions, never before
  the provider's history floor. Before: a lead-in cut short by the floor, the symbol's
  listing date, a gap or sparse sessions was computed on without a word. Now: the
  sessions the sizing names are admitted with the window (counted apart as warmup) and
  the result says how many bars the lead-in really held (:class:`WarmupReport`).
  ``warmup_policy`` decides what a shortfall may do. ``allow_shortened`` (the default,
  what the study always did, now reported) computes on the lead-in that exists — the
  oldest lead-in sessions the provider cannot supply are dropped and counted.
  ``require_full`` refuses with ``warmup_shortened``. A hole *after* the first
  admitted lead-in session is refused whatever the policy: the indicator will not run
  across a missing session silently.
* **Look-forward.** Horizons of 1 to 100 bars (default 1, 5, 10, 15, 30). A forward
  return that crosses a UTC date is masked, nothing is read past ``to_date`` so the last
  bars of the window have no label, and the IC decay curve runs to the largest horizon
  plus ten. Unchanged.
* **Guards.** "Insufficient data" (fewer bars than lookback plus the largest horizon,
  and fewer than 100 bars after the warmup is trimmed) are unchanged except that the
  first now counts the regular-hours bars the study uses, where it counted every bar
  the provider served.
* **Cache and result.** None before, none now: the study is synchronous and has never
  cached, and its random baseline is an unseeded Monte Carlo draw, so a stored result
  would freeze a different number than a rerun gives. What the response carries instead
  is the identity a cache or a saved result would key on (:func:`result_identity`): the
  input identity, the admitted snapshot's digest, the data root and the calculation
  version, under :data:`RESULT_NAMESPACE`. A repaired lake gives the same request a new
  snapshot digest; a changed policy gives it a new identity digest.

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

from app.data_lake.polygon_fetcher import polygon_history_floor
from app.lean_sidecar.trading_calendar import expected_sessions
from app.models.indicator_reliability_models import WarmupPolicy, WarmupShortfallCause
from app.research.indicator_reliability import DEFAULT_VOL_WINDOW, MAX_DECAY_HORIZON, RANDOM_SIMULATIONS, TRAIN_RATIO
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import (
    AdmittedMinuteInputs,
    MinuteWindowIdentity,
    StudyCoverage,
    StudyInputRefusal,
    admit_minute_inputs,
    bars_as_rows,
    fold_minute_bars,
    lead_in_sessions,
)
from app.services.dataset_service import INDICATOR_CONFIGS, bar_minutes_for, resolve_indicator_window
from app.services.indicator_warmup_policy import requested_indicator_warmup_lookback
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Bump when the IC, correction, baseline, regime or IR-proxy math changes: a result is the product of the code that
#: made it, and this version is what tells a result of the new math apart from one of the old.
RELIABILITY_CALCULATION_VERSION = "indicator-reliability/1"

#: The namespace a result cache or saved result for lake-sourced reliability studies would live under. Nothing is
#: stored in it today; the identity digests are computed under it so a later cache cannot collide with another study's.
RESULT_NAMESPACE = "indicator_reliability:lake:v1"

#: The fold rule named in the identity: what a coarser bar is made of.
FOLD_RULE = "minute_fold_et_wall_clock_no_fill"

_REQUESTER = "indicator-reliability"

#: Minute bar lengths that start every bar on a wall-clock boundary (they divide the hour).
SUPPORTED_MINUTE_MULTIPLIERS: tuple[int, ...] = tuple(m for m in range(1, 61) if 60 % m == 0)

ReliabilityRefusalCode = Literal[
    "resolution_unsupported",
    "symbol_not_addressable",
    "no_sessions_in_window",
    "window_not_complete",
    "data_unavailable_from_provider",
    "data_corrupt",
    "data_building",
    "data_missing",
    "lake_unavailable",
    "warmup_shortened",
]

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
    price_adjustment_mode: Literal["raw", "polygon_split_adjusted"] = "polygon_split_adjusted"


@dataclass(frozen=True)
class ReliabilityIdentity:
    """Every input that decides this study's numbers: the lake input plus how the study shapes it."""

    inputs: MinuteWindowIdentity
    timespan: str
    multiplier: int
    bar_minutes: int
    indicator_name: str
    indicator_params: tuple[tuple[str, int | float], ...]
    horizons: tuple[int, ...]
    include_slope: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.inputs.as_dict(),
            "timespan": self.timespan,
            "multiplier": self.multiplier,
            "bar_minutes": self.bar_minutes,
            "aggregation": FOLD_RULE,
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

    def digest(self) -> str:
        return hash_payload(self.as_dict())


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


# ---------------------------------------------------------------------------
# Refusal, warmup
# ---------------------------------------------------------------------------


class ReliabilityRefusal(StudyInputRefusal):
    """No result: the study's required input was not admitted, or the request names an input it cannot read.

    A :class:`StudyInputRefusal` (admission's own) plus the study's ``warmup_shortened`` and the data-source
    block that says what was reached, so the response can show the counts beside the reason.
    """

    def __init__(
        self,
        code: ReliabilityRefusalCode,
        reason: str,
        *,
        coverage: StudyCoverage | None = None,
        data_source: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, reason, coverage=coverage)
        self.data_source = data_source

    def as_detail(self) -> dict[str, Any]:
        """The refusal as an HTTP error detail: ``message`` is the sentence, ``data_source`` what admission reached."""
        body = self.as_dict()
        body["message"] = body.pop("reason")
        body["data_source"] = self.data_source
        return body


@dataclass(frozen=True)
class WarmupReport:
    """The warmup lead-in the policy asked for against the one the lake supplied.

    ``requested_bars`` is the policy's own number (lookback times the multiplier); ``requested_sessions`` the
    scheduled sessions its sizing named, never before the provider floor. ``admitted_*`` is what the study ran on.
    ``cause`` says why a shortfall happened, first match winning: the provider could not supply the oldest
    lead-in sessions (the symbol had no bars yet), the sizing was limited by the provider floor, or every named
    session was admitted but held fewer bars than a fully traded session.
    """

    policy: WarmupPolicy
    requested_bars: int
    requested_sessions: int
    admitted_sessions: int
    admitted_bars: int
    shortened: bool
    shortfall_bars: int
    cause: WarmupShortfallCause

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "requested_bars": self.requested_bars,
            "requested_sessions": self.requested_sessions,
            "admitted_sessions": self.admitted_sessions,
            "admitted_bars": self.admitted_bars,
            "shortened": self.shortened,
            "shortfall_bars": self.shortfall_bars,
            "cause": self.cause,
        }


_CAUSE_WORDS: dict[WarmupShortfallCause, str] = {
    "none": "no shortfall",
    "provider_history_floor": "the provider's history does not reach back further",
    "symbol_history_starts": "the symbol has no bars before its first listed session",
    "sparse_session_bars": "the lead-in sessions hold fewer bars than a fully traded session",
}


def warmup_report(
    policy: WarmupPolicy,
    *,
    requested_bars: int,
    requested_sessions: int,
    admitted_sessions: int,
    admitted_bars: int,
    floor_limited: bool,
) -> WarmupReport:
    shortfall = max(requested_bars - admitted_bars, 0)
    cause: WarmupShortfallCause
    if shortfall == 0:
        cause = "none"
    elif admitted_sessions < requested_sessions:
        cause = "symbol_history_starts"
    elif floor_limited:
        cause = "provider_history_floor"
    else:
        cause = "sparse_session_bars"
    return WarmupReport(
        policy=policy,
        requested_bars=requested_bars,
        requested_sessions=requested_sessions,
        admitted_sessions=admitted_sessions,
        admitted_bars=admitted_bars,
        shortened=shortfall > 0,
        shortfall_bars=shortfall,
        cause=cause,
    )


# ---------------------------------------------------------------------------
# Result identity and provenance
# ---------------------------------------------------------------------------


def result_identity(identity: ReliabilityIdentity, admitted: AdmittedMinuteInputs) -> dict[str, Any]:
    """What a stored result of this study is a result *of*: data policy, admitted bytes, inputs and the code."""
    return {
        "namespace": RESULT_NAMESPACE,
        "identity": identity.as_dict(),
        "data_root_id": str(admitted.data_root_id),
        "snapshot_digest": admitted.snapshot_digest,
        "calculation_version": RELIABILITY_CALCULATION_VERSION,
    }


def data_source_block(
    identity: ReliabilityIdentity,
    *,
    warmup: WarmupReport | None,
    coverage: StudyCoverage | None,
    admitted: AdmittedMinuteInputs | None = None,
    bars_used: int | None = None,
) -> dict[str, Any]:
    """The provenance a data-source line shows: the identity read, the admitted bytes, and the coverage counts."""
    inputs = identity.inputs.as_dict()
    return {
        "kind": "lake",
        "provider": inputs["provider"],
        "price_adjustment_mode": inputs["price_adjustment_mode"],
        "session_policy": inputs["session_policy"],
        "bar_minutes": identity.bar_minutes,
        "aggregation": FOLD_RULE,
        "window_start_ms": inputs["window_start_ms"],
        "window_end_ms": inputs["window_end_ms"],
        "warmup": warmup.as_dict() if warmup is not None else None,
        "calculation_version": RELIABILITY_CALCULATION_VERSION,
        "identity_digest": identity.digest(),
        "snapshot_digest": admitted.snapshot_digest if admitted is not None else None,
        "result_identity_digest": hash_payload(result_identity(identity, admitted)) if admitted is not None else None,
        "data_root_id": str(admitted.data_root_id) if admitted is not None else None,
        "corporate_action_versions": dict(admitted.snapshot.corporate_action_versions) if admitted is not None else {},
        "coverage": coverage.as_dict() if coverage is not None else None,
        "minute_bars_read": len(admitted.warmup_bars) + len(admitted.window_bars) if admitted is not None else None,
        "bars_used": bars_used,
    }


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class AdmittedReliabilityInputs:
    """The study's bars — warmup lead-in and window, regular hours, at its resolution — with their receipt.

    ``frame`` holds ``timestamp`` (bar start, ``int64 ms UTC``), ``open``, ``high``, ``low``, ``close`` and
    ``volume``. Rows before ``window_start_ms`` (ET midnight of ``from_date``) are the warmup lead-in: the
    indicator runs over them and the caller trims them.
    """

    identity: ReliabilityIdentity
    frame: pd.DataFrame
    window_start_ms: int
    max_lookback: int
    warmup: WarmupReport
    admitted: AdmittedMinuteInputs

    def data_source(self, *, bars_used: int | None = None) -> dict[str, Any]:
        return data_source_block(
            self.identity,
            warmup=self.warmup,
            coverage=self.admitted.coverage,
            admitted=self.admitted,
            bars_used=bars_used,
        )


def _shorter_lead_in(refusal: StudyInputRefusal, inputs: MinuteWindowIdentity) -> int | None:
    """The lead-in length that drops the oldest sessions the provider cannot supply, or ``None`` for any other refusal.

    Only a refusal whose every failed session is an unsupplied lead-in session, and whose failed sessions are the
    oldest ones, is a symbol whose history starts inside the lead-in. A hole after an admitted session is a gap, and
    a corrupt, building or window session is not a shortfall at all: none of those are trimmed away.
    """
    coverage = refusal.coverage
    if coverage is None or not coverage.issues or inputs.warmup_sessions == 0:
        return None
    if any(issue.group != "warmup" or issue.outcome != "unavailable_from_provider" for issue in coverage.issues):
        return None
    window = expected_sessions(inputs.window_start, inputs.window_end)
    lead = lead_in_sessions(window[0], inputs.warmup_sessions)
    failed = {issue.trading_date for issue in coverage.issues}
    oldest = 0
    while oldest < len(lead) and lead[oldest] in failed:
        oldest += 1
    return len(lead) - oldest if oldest == len(failed) else None


def _coverage_without_dropped_lead_in(first: StudyCoverage, *, kept: int) -> StudyCoverage:
    """Coverage as the first attempt saw it, less the lead-in sessions the provider has no bars for.

    The attempt that reads the shortened lead-in finds every session already held — the first attempt filled them —
    so its own counts would file this run's work under "reused". The first attempt's counts are the true ones.
    """
    return replace(first, warmup=replace(first.warmup, expected=kept, unavailable_from_provider=0), issues=())


def _admit(
    inputs: MinuteWindowIdentity, policy: WarmupPolicy, now: int
) -> tuple[MinuteWindowIdentity, AdmittedMinuteInputs]:
    """Admit the identity; under ``allow_shortened`` retry once without the lead-in sessions the provider has no bars for."""
    try:
        return inputs, admit_minute_inputs(inputs, requester=_REQUESTER, now_ms=now)
    except StudyInputRefusal as refusal:
        shorter = _shorter_lead_in(refusal, inputs) if policy == "allow_shortened" else None
        if shorter is None or refusal.coverage is None:
            raise
        logger.info(
            "reliability warmup lead-in starts after its first sessions",
            extra={
                "action": "reliability_warmup_prefix_dropped",
                "symbol": inputs.symbol,
                "requested_sessions": inputs.warmup_sessions,
                "kept_sessions": shorter,
            },
        )
        trimmed = replace(inputs, warmup_sessions=shorter)
        retried = admit_minute_inputs(trimmed, requester=_REQUESTER, now_ms=now)
        return trimmed, replace(retried, coverage=_coverage_without_dropped_lead_in(refusal.coverage, kept=shorter))


def _lead_in_aware(refusal: StudyInputRefusal, from_date: date) -> str:
    """Say that the failed sessions are the warmup's when every one of them is."""
    coverage = refusal.coverage
    if coverage is None or not coverage.issues or any(issue.group != "warmup" for issue in coverage.issues):
        return refusal.reason
    return f"{refusal.reason} Every failed session lies in the warmup lead-in before {from_date.isoformat()}."


def admit_reliability_inputs(params: ReliabilityParams, *, now_ms: int | None = None) -> AdmittedReliabilityInputs:
    """Size the warmup, admit the lake inputs for it and the window, and hand over the study's bars — or refuse.

    Blocking: call from a worker thread (materialization and byte admission cross to the shared background loop).
    Raises :class:`ReliabilityRefusal`; nothing is computed on a series that admission did not complete, and a
    shortfall in the lead-in is either reported or refused, never silent.
    """
    bar_minutes = study_bar_minutes(params.timespan, params.multiplier)
    now = now_ms_utc() if now_ms is None else now_ms

    indicator_entry = {"name": params.indicator_name, "params": dict(params.indicator_params)}
    max_lookback = requested_indicator_warmup_lookback([indicator_entry], INDICATOR_CONFIGS)
    window = resolve_indicator_window(
        params.from_date.isoformat(),
        max_lookback=max_lookback,
        bar_minutes=bar_minutes_for(params.timespan, params.multiplier),
    )
    fetch_from = date.fromisoformat(window.fetch_from)
    lead = expected_sessions(fetch_from, params.from_date - timedelta(days=1)) if fetch_from < params.from_date else []

    def identity_for(inputs: MinuteWindowIdentity) -> ReliabilityIdentity:
        return ReliabilityIdentity(
            inputs=inputs,
            timespan=params.timespan,
            multiplier=params.multiplier,
            bar_minutes=bar_minutes,
            indicator_name=params.indicator_name,
            indicator_params=tuple(sorted(params.indicator_params.items())),
            horizons=params.horizons,
            include_slope=params.include_slope,
        )

    requested_inputs = MinuteWindowIdentity(
        symbol=params.symbol,
        price_adjustment_mode=params.price_adjustment_mode,
        session_policy="regular",
        window_start=params.from_date,
        window_end=params.to_date,
        warmup_sessions=len(lead),
    )
    try:
        admitted_inputs, admitted = _admit(requested_inputs, params.warmup_policy, now)
    except StudyInputRefusal as refusal:
        raise ReliabilityRefusal(
            refusal.code,
            _lead_in_aware(refusal, params.from_date),
            coverage=refusal.coverage,
            data_source=data_source_block(identity_for(requested_inputs), warmup=None, coverage=refusal.coverage),
        ) from refusal

    identity = identity_for(admitted_inputs)
    frame = pd.DataFrame(
        bars_as_rows(fold_minute_bars((*admitted.warmup_bars, *admitted.window_bars), bar_minutes)),
        columns=_FRAME_COLUMNS,
    )
    admitted_bars = int((frame["timestamp"] < window.window_start_ms).sum())
    report = warmup_report(
        params.warmup_policy,
        requested_bars=window.warmup_bars,
        requested_sessions=len(lead),
        admitted_sessions=admitted_inputs.warmup_sessions,
        admitted_bars=admitted_bars,
        floor_limited=fetch_from <= polygon_history_floor(et_date_at_ms(now)),
    )
    if report.shortened:
        logger.info(
            "reliability warmup shorter than the policy asks",
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
            f"{report.admitted_bars} ({_CAUSE_WORDS[report.cause]}). Start the window later, or allow a shortened warmup.",
            coverage=admitted.coverage,
            data_source=data_source_block(identity, warmup=report, coverage=admitted.coverage, admitted=admitted),
        )
    return AdmittedReliabilityInputs(
        identity=identity,
        frame=frame,
        window_start_ms=window.window_start_ms,
        max_lookback=max_lookback,
        warmup=report,
        admitted=admitted,
    )

