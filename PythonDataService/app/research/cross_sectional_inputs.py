"""Admitted lake inputs for the cross-sectional study: stock bars and option inputs (#3524, #3540; PRD #3490 R12-R15, R25, T05, T09, T18).

The study's input contract, field by field and before/after, is the docstring of
:mod:`app.services.cross_sectional_study_service`. This module is the lake side of it.

* **Identity** (:class:`CrossSectionalIdentity`): the universe, data kind, adjustment, session policy, window and
  fold rule, and the policy the option inputs are read under; each member's minute-bar input is
  :meth:`CrossSectionalIdentity.member`. The coverage query and the compute read exactly that identity.
* **Admission** (:func:`admit_universe`): each member through :func:`app.research.study_inputs.admit_minute_inputs` —
  the existing materializer (catalog claims, lease coordination) and committed-byte admission. The UI is never a writer.
  Each member is admitted twice: split-adjusted, which the target is measured on, and raw, whose daily close is the spot
  its options were struck against (the IV spot and the strike picks; ADR 0078 decision 6). Then
  (:func:`admit_option_inputs`) each admitted member's option inputs: its listings and contract bars, saved into the lake
  first if the lake lacks them (:mod:`app.research.options.option_inputs`).
* **Daily rows** (:func:`daily_rows`): the lake holds minute bars, so a daily bar is a fold of the admitted minutes of
  one New York session, by the lake's own reduction (:func:`app.data_lake.derived_daily.aggregate_minute_to_daily`) —
  not the whole-symbol daily rollup. The rollup is rebuilt whenever a symbol's catalogued coverage changes, offers no
  session choice, and is not part of a per-session receipt. Under the ``extended`` policy the study's rows equal the
  rollup's rows (``tests/research/test_cross_sectional_inputs.py``); under ``regular``, the study's session, they fold the
  scheduled session ``[open, close)``, which leaves out the minute stamped 16:00. Against the vendor's own daily bars for
  SPY, 2025-01-13 to 17, the open, high and low are exact; the close differs by up to $0.15 (the vendor's is the official
  print, the fold's is the last regular minute, the instant the option minute bars price at) and the fold holds 71.5 to
  84.7 percent of the vendor's volume (golden fixture ``SPY-DAILY-001``).
* **Partial universes** (T18): a member the lake cannot admit, in its stock bars or in its option inputs, is a
  :class:`MemberExclusion` with its stage, code, reason and coverage, not a dropped symbol. When no member can be admitted
  the study refuses.

Time: every temporal value leaving this module is ``int64 ms UTC``; a daily row is stamped with its session's open.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.data_lake.derived_daily import aggregate_minute_to_daily
from app.data_lake.lean_writer import MinuteTradeBar
from app.data_lake.option_lake import option_capture_opens_ms
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options_backtest.fetching import LakeOptionCapture
from app.research.options.option_inputs import (
    MemberOptionInputs,
    OptionInputRefusal,
    admit_member_option_inputs,
    option_input_identity,
)
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import (
    MAX_LISTED_SESSIONS,
    AdjustmentMode,
    AdmittedMinuteInputs,
    GroupCoverage,
    MinuteWindowIdentity,
    RefusalCode,
    SessionPolicy,
    StudyCoverage,
    StudyInputRefusal,
    admit_minute_inputs,
)
from app.research.study_universe import MemberExclusion, members_summary
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

#: The fold rule named in the identity: what a daily bar is made of.
DAILY_FOLD_RULE = "minute_fold_per_et_session_no_fill"

#: What a daily row's ``timestamp`` is.
BAR_TIMESTAMP_ANCHOR = "session_open"

#: Refusals no second attempt can change: the request itself cannot be served.
_STRUCTURAL_CODES: frozenset[RefusalCode] = frozenset(
    {"symbol_not_addressable", "no_sessions_in_window", "window_not_complete", "resolution_unsupported"}
)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossSectionalIdentity:
    """Every input that decides this study's stock-side numbers: the universe, and the lake input read for each member."""

    symbols: tuple[str, ...]
    price_adjustment_mode: AdjustmentMode
    session_policy: SessionPolicy
    window_start: date
    window_end: date

    def member(self, symbol: str) -> MinuteWindowIdentity:
        """The minute-bar input admission reads for one member (the lake's resolution; the fold is the study's)."""
        return MinuteWindowIdentity(
            symbol=symbol,
            price_adjustment_mode=self.price_adjustment_mode,
            session_policy=self.session_policy,
            window_start=self.window_start,
            window_end=self.window_end,
            warmup_sessions=0,
        )

    def as_dict(self) -> dict[str, Any]:
        """The identity as it travels: bounds are ET-midnight ``int64 ms UTC``, members sorted (order is not science)."""
        return {
            "data_kind": "trade_bars",
            "provider": "polygon",
            "base_resolution": "minute",
            "bar_resolution": "day",
            "aggregation": DAILY_FOLD_RULE,
            "bar_timestamp_anchor": BAR_TIMESTAMP_ANCHOR,
            "price_adjustment_mode": self.price_adjustment_mode,
            "session_policy": self.session_policy,
            "window_start_ms": et_midnight_ms(self.window_start),
            "window_end_ms": et_midnight_ms(self.window_end),
            "warmup_sessions": 0,
            "look_forward_extends_window": False,
            "symbols": sorted(self.symbols),
            "option_inputs": option_input_identity(),
        }

    def digest(self) -> str:
        return hash_payload(self.as_dict())


def cross_sectional_identity(
    symbols: Sequence[str],
    from_date: date,
    to_date: date,
    *,
    timespan: str,
    multiplier: int,
    session_policy: SessionPolicy,
    price_adjustment_mode: AdjustmentMode = "polygon_split_adjusted",
) -> CrossSectionalIdentity:
    """The study's input identity, or a typed refusal for a resolution it can never run on."""
    if timespan != "day" or multiplier != 1:
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"The cross-sectional study runs on daily bars only (its targets look one and five sessions ahead); "
            f"{multiplier}-{timespan} bars were requested. Use daily bars.",
        )
    return CrossSectionalIdentity(
        symbols=tuple(symbols),
        price_adjustment_mode=price_adjustment_mode,
        session_policy=session_policy,
        window_start=from_date,
        window_end=to_date,
    )


# ---------------------------------------------------------------------------
# Daily bars from admitted minutes
# ---------------------------------------------------------------------------


def daily_rows(bars: Iterable[TradeBar]) -> list[dict[str, float | int]]:
    """Daily study rows from admitted minute bars: one per New York session that holds a minute.

    Formula: per session date, open = the first minute's open, high = max of highs, low = min of lows, close = the
    last minute's close, volume = sum of volumes, over the minutes the session holds (the reader has already applied
    the session policy). A session holding no minute yields no row; nothing is filled. ``timestamp`` is the session
    open, ``int64 ms UTC``; prices and volume are float64 of the exact Decimals.
    Reference: ``app/data_lake/derived_daily.py`` (the lake's own minute-to-daily reduction, ADR 0049 section 4.6).
    Canonical implementation: ``app/data_lake/derived_daily.py::aggregate_minute_to_daily`` (this function drives it).
    Validated against: tests/research/test_cross_sectional_inputs.py::test_daily_rows_equal_the_lakes_own_daily_rollup; the vendor's real daily bars
    for one SPY week, golden fixture SPY-DAILY-001, by tests/research/test_cross_sectional_lake_vs_provider.py::test_the_regular_fold_of_a_real_spy_week_equals_the_vendors_real_daily_open_high_and_low
    """
    minutes = [
        MinuteTradeBar(
            bar_start_et=datetime.fromtimestamp(bar.start_ms / 1000, _ET),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=Decimal(bar.volume),
        )
        for bar in bars
    ]
    return [
        {
            "timestamp": session_open_ms_utc(day.trading_date),
            "open": float(day.open),
            "high": float(day.high),
            "low": float(day.low),
            "close": float(day.close),
            "volume": float(day.volume),
        }
        for day in aggregate_minute_to_daily(minutes)
    ]


# ---------------------------------------------------------------------------
# Admission of the universe
# ---------------------------------------------------------------------------


def exclusion_from_refusal(ticker: str, refusal: StudyInputRefusal) -> MemberExclusion:
    return MemberExclusion(
        ticker=ticker,
        stage="stock_bars",
        code=refusal.code,
        reason=refusal.reason,
        retryable=refusal.code not in _STRUCTURAL_CODES,
        refusal=refusal.as_dict(),
    )


def exclusion_from_option_refusal(ticker: str, refusal: OptionInputRefusal) -> MemberExclusion:
    return MemberExclusion(
        ticker=ticker,
        stage="option_inputs",
        code=refusal.code,
        reason=refusal.reason,
        retryable=refusal.retryable,
        refusal=refusal.as_dict(),
    )


@dataclass(frozen=True)
class AdmittedMember:
    """One member's admitted stock bars: the daily rows to compute on, and the receipt of the bytes they came from.

    ``rows`` are split-adjusted (the target is measured on them); ``spot_rows`` are the same sessions raw, whose close is
    the price the member's options were struck against, and ``raw_admitted`` the receipt of the raw bytes.
    """

    ticker: str
    admitted: AdmittedMinuteInputs
    rows: list[dict[str, float | int]]
    identity_digest: str
    raw_admitted: AdmittedMinuteInputs
    spot_rows: list[dict[str, float | int]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "status": "admitted",
            "identity_digest": self.identity_digest,
            "snapshot_digest": self.admitted.snapshot_digest,
            "raw_snapshot_digest": self.raw_admitted.snapshot_digest,
            "data_root_id": str(self.admitted.data_root_id),
            "corporate_action_versions": dict(self.admitted.snapshot.corporate_action_versions),
            "coverage": self.admitted.coverage.as_dict(),
            "minute_bars_read": len(self.admitted.window_bars),
            "daily_bars": len(self.rows),
        }


@dataclass
class UniverseAdmission:
    """What admission did for every requested member, in request order.

    ``members`` are those whose stock bars were admitted, and ``excluded`` those whose stock bars were not.
    ``option_inputs`` are the admitted members whose option inputs were admitted too, and ``option_excluded`` (with the
    refusal behind each in ``option_refusals``) those whose option inputs were not: a member is computed only when it is
    in both.
    """

    members: dict[str, AdmittedMember] = field(default_factory=dict)
    excluded: dict[str, MemberExclusion] = field(default_factory=dict)
    #: Coverage of every member admission started on, admitted or refused, for the data-source line.
    coverage: dict[str, StudyCoverage] = field(default_factory=dict)
    option_inputs: dict[str, MemberOptionInputs] = field(default_factory=dict)
    option_excluded: dict[str, MemberExclusion] = field(default_factory=dict)
    option_refusals: dict[str, OptionInputRefusal] = field(default_factory=dict)

    def computable(self) -> dict[str, AdmittedMember]:
        """The members with both their stock bars and their option inputs, in request order."""
        return {ticker: member for ticker, member in self.members.items() if ticker in self.option_inputs}

    def stock_bars(self) -> dict[str, list[dict[str, float | int]]]:
        return {ticker: member.rows for ticker, member in self.computable().items()}

    def every_exclusion(self) -> dict[str, MemberExclusion]:
        """Every member left out before the compute, at either stage."""
        return {**self.excluded, **self.option_excluded}


def admit_universe(
    identity: CrossSectionalIdentity,
    *,
    requester: str,
    now_ms: int | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    on_member_start: Callable[[int, int, str], None] = lambda index, total, ticker: None,
    on_member: Callable[[int, int, str, str], None] = lambda index, total, ticker, outcome: None,
) -> UniverseAdmission:
    """Admit each member's stock bars in turn; a member the lake cannot admit is excluded with the reason, not dropped.

    Blocking: call from a worker thread. Members are admitted one after another — the materializer coordinates
    concurrent writers and the provider's budget, and a cold universe is many sessions per member. A member's bars are
    admitted split-adjusted and then raw (the raw close is the spot of its options); either refusal excludes it. A
    cancellation raised by ``check_cancelled`` propagates; only a typed refusal excludes a member. ``on_member_start`` is
    called before each member (a cold one can take minutes in the materializer, which a cancel does not interrupt) and
    ``on_member`` after it with the outcome.
    """
    admission = UniverseAdmission()
    total = len(identity.symbols)
    for index, ticker in enumerate(identity.symbols):
        check_cancelled()
        on_member_start(index, total, ticker)
        member_identity = identity.member(ticker)
        try:
            admitted = admit_minute_inputs(
                member_identity, requester=requester, now_ms=now_ms, check_cancelled=check_cancelled
            )
        except StudyInputRefusal as refusal:
            logger.warning(
                "cross-sectional member not admitted",
                extra={"action": "cross_sectional_member_excluded", "ticker": ticker, "code": refusal.code},
            )
            admission.excluded[ticker] = exclusion_from_refusal(ticker, refusal)
            if refusal.coverage is not None:
                admission.coverage[ticker] = refusal.coverage
            on_member(index, total, ticker, f"excluded ({refusal.code})")
            continue
        try:
            raw = admit_minute_inputs(
                replace(member_identity, price_adjustment_mode="raw"),
                requester=requester,
                now_ms=now_ms,
                check_cancelled=check_cancelled,
            )
        except StudyInputRefusal as refusal:
            logger.warning(
                "cross-sectional member's raw bars not admitted",
                extra={"action": "cross_sectional_member_excluded", "ticker": ticker, "code": refusal.code},
            )
            admission.excluded[ticker] = exclusion_from_refusal(
                ticker,
                StudyInputRefusal(
                    refusal.code,
                    f"The unadjusted bars its options are priced against: {refusal.reason}",
                    coverage=refusal.coverage,
                ),
            )
            admission.coverage[ticker] = admitted.coverage
            on_member(index, total, ticker, f"excluded ({refusal.code})")
            continue
        admission.members[ticker] = AdmittedMember(
            ticker=ticker,
            admitted=admitted,
            rows=daily_rows(admitted.window_bars),
            identity_digest=hash_payload(member_identity.as_dict()),
            raw_admitted=raw,
            spot_rows=daily_rows(raw.window_bars),
        )
        admission.coverage[ticker] = admitted.coverage
        on_member(index, total, ticker, "admitted")
    return admission


def option_window_refusal(identity: CrossSectionalIdentity, *, now_ms: int | None = None) -> StudyInputRefusal | None:
    """The refusal for a window whose last session is too recent for the lake to take its option data, or ``None``.

    The lake takes a session's option data only from New York midnight after its date (ADR 0078 decision 14), later than
    the stock bars of the same session are final; a study cannot save the option inputs of a session it may not capture, so
    it refuses before it touches the lake.
    """
    sessions = expected_sessions(identity.window_start, identity.window_end)
    if not sessions:
        return None  # every member's admission says so, with the window it was asked for
    now = now_ms_utc() if now_ms is None else now_ms
    opens_ms = option_capture_opens_ms(sessions[-1])
    if now >= opens_ms:
        return None
    return StudyInputRefusal(
        "window_not_complete",
        f"The last session of the window, {sessions[-1].isoformat()}, is not over in New York yet. The lake takes a "
        "session's option data only from New York midnight after it, and the study saves the option inputs it needs. End "
        "the window on an earlier session, or run it after midnight.",
    )


def admit_option_inputs(
    identity: CrossSectionalIdentity,
    admission: UniverseAdmission,
    *,
    now_ms: int,
    check_cancelled: Callable[[], None] = lambda: None,
    should_stop: Callable[[], bool] = lambda: False,
    on_member_start: Callable[[int, int, str], None] = lambda index, total, ticker: None,
    on_member: Callable[[int, int, str, str], None] = lambda index, total, ticker, outcome: None,
    on_progress: Callable[[int, int, str], None] = lambda done, total, message: None,
) -> None:
    """Admit the option inputs of each member whose stock bars were admitted, saving into the lake what it lacks.

    Blocking: call from a worker thread. Members are admitted one after another, so the study holds at most the capture
    bound of :class:`LakeOptionCapture` and leaves the rest of the process's vendor gate to the nightly archive. A
    member whose option inputs cannot be supplied is excluded (stage ``option_inputs``) with the issues behind it and
    nothing of it is computed. ``check_cancelled`` raises the job's cancellation and ``should_stop`` says whether it was
    asked for, which a wait on the lake polls (both consult the job, which renews its lease): a cancellation propagates.
    """
    members = list(admission.members.values())
    total = len(members)
    for index, member in enumerate(members):
        check_cancelled()
        ticker = member.ticker
        on_member_start(index, total, ticker)
        capture = LakeOptionCapture(ticker, now_ms=now_ms, should_stop=should_stop)
        try:
            admission.option_inputs[ticker] = admit_member_option_inputs(
                ticker,
                spot_rows=member.spot_rows,
                start_date=identity.window_start,
                end_date=identity.window_end,
                now_ms=now_ms,
                capture=capture,
                check_cancelled=check_cancelled,
                on_progress=on_progress,
            )
        except OptionInputRefusal as refusal:
            logger.warning(
                "cross-sectional member's option inputs not admitted",
                extra={"action": "cross_sectional_member_excluded", "ticker": ticker, "code": refusal.code},
            )
            admission.option_refusals[ticker] = refusal
            admission.option_excluded[ticker] = exclusion_from_option_refusal(ticker, refusal)
            on_member(index, total, ticker, f"excluded ({refusal.code})")
            continue
        on_member(index, total, ticker, "admitted")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def sum_groups(groups: Iterable[GroupCoverage]) -> GroupCoverage:
    """Session counts added up over members: ``expected`` is member-sessions, not distinct dates."""
    groups = list(groups)
    return GroupCoverage(
        expected=sum(g.expected for g in groups),
        reused=sum(g.reused for g in groups),
        added=sum(g.added for g in groups),
        missing=sum(g.missing for g in groups),
        building=sum(g.building for g in groups),
        corrupt=sum(g.corrupt for g in groups),
        captured_before_final=sum(g.captured_before_final for g in groups),
        unavailable_from_provider=sum(g.unavailable_from_provider for g in groups),
    )


def universe_coverage(admission: UniverseAdmission) -> StudyCoverage:
    """What admission did for the whole universe, as the one :class:`StudyCoverage` the shared data-source line reads.

    Each expected unit is one member-session. Every session a member could not make ready is kept, its reason led by
    the member's ticker, so a refusal names whose session it was.
    """
    members = admission.coverage
    return StudyCoverage(
        window=sum_groups(c.window for c in members.values()),
        warmup=sum_groups(c.warmup for c in members.values()),
        artifacts_fetched=sum(c.artifacts_fetched for c in members.values()),
        artifacts_reused=sum(c.artifacts_reused for c in members.values()),
        artifacts_refreshed=sum(c.artifacts_refreshed for c in members.values()),
        issues=tuple(
            replace(issue, reason=f"{ticker}: {issue.reason}") for ticker, c in members.items() for issue in c.issues
        ),
    )


def universe_refusal(identity: CrossSectionalIdentity, admission: UniverseAdmission) -> StudyInputRefusal:
    """The refusal a universe with no admitted member ends with: the most specific code, and each member's reason."""
    excluded = admission.every_exclusion()
    return StudyInputRefusal(
        refusal_code_for(excluded),
        f"The lake cannot admit stock bars or option inputs for any of the {len(identity.symbols)} requested members. "
        f"{members_summary([(e.ticker, e.reason) for e in excluded.values()])}",
        coverage=universe_coverage(admission),
    )


def _universe_label(symbols: Sequence[str], *, shown: int = 5) -> str:
    listed = ", ".join(symbols[:shown])
    return listed if len(symbols) <= shown else f"{listed} and {len(symbols) - shown} more"


def stock_bars_block(
    identity: CrossSectionalIdentity,
    admission: UniverseAdmission,
    *,
    calculation_version: str,
    cache_hit: bool = False,
    cached_at_ms: int | None = None,
) -> dict[str, Any]:
    """The data-source entry for the stock bars: the lake, the identity read, and what admission did per member.

    It carries every key of :func:`app.research.lake_study.data_source_block`, so the browser reads it with the same
    mapper as any lake study's. What differs is that a universe has no single value for some of them: ``symbol`` is the
    universe's label, ``bar_minutes`` is ``None`` (a daily bar is a session fold, not a number of minutes),
    ``snapshot_digest`` is the digest of the admitted members' snapshot digests (it changes when any one member's bytes
    do; ``raw_snapshot_digest`` is the same over the raw bars the options are struck against), ``effective_window`` spans
    the first and last session any member admitted, and ``coverage`` counts member-sessions of the adjusted bars.
    ``members`` adds each member's own receipt or exclusion. A member whose option inputs were refused still shows here
    as admitted: its stock bars were; the option block names its exclusion. The option inputs' own policy is that block's.
    """
    members: list[dict[str, Any]] = []
    for ticker in identity.symbols:
        if ticker in admission.members:
            members.append(admission.members[ticker].as_dict())
        else:
            excluded = admission.excluded[ticker]
            held = admission.coverage.get(ticker)
            members.append(
                {
                    "ticker": ticker,
                    "status": "excluded",
                    "code": excluded.code,
                    "reason": excluded.reason,
                    "coverage": held.as_dict() if held is not None else None,
                    "sessions": excluded.refusal["sessions"][:MAX_LISTED_SESSIONS] if excluded.refusal else [],
                    "sessions_total": excluded.refusal["sessions_total"] if excluded.refusal else 0,
                }
            )
    admitted = list(admission.members.values())
    snapshots = sorted((m.ticker, m.admitted.snapshot_digest) for m in admitted)
    sessions = sorted({day for m in admitted for day in m.admitted.window_sessions})
    raw_snapshots = sorted((m.ticker, m.raw_admitted.snapshot_digest) for m in admitted)
    return {
        "kind": "lake",
        **{key: value for key, value in identity.as_dict().items() if key != "option_inputs"},
        "symbol": _universe_label(identity.symbols),
        "bar_minutes": None,
        "identity_digest": identity.digest(),
        "calculation_version": calculation_version,
        "snapshot_digest": hash_payload(snapshots) if snapshots else None,
        "raw_snapshot_digest": hash_payload(raw_snapshots) if raw_snapshots else None,
        "effective_window": (
            {"start_ms": et_midnight_ms(sessions[0]), "end_ms": et_midnight_ms(sessions[-1])} if sessions else None
        ),
        "coverage": universe_coverage(admission).as_dict(),
        "minute_bars_read": sum(len(m.admitted.window_bars) for m in admitted),
        "bars_used": sum(len(m.rows) for m in admitted),
        "cache": {"hit": cache_hit, "cached_at_ms": cached_at_ms},
        "members": members,
    }


def refusal_code_for(exclusions: Mapping[str, MemberExclusion]) -> RefusalCode:
    """The one code a refused universe answers with: the most specific cause any member shows."""
    order: tuple[RefusalCode, ...] = (
        "window_not_complete",
        "symbol_not_addressable",
        "no_sessions_in_window",
        "data_unavailable_from_provider",
        "data_corrupt",
        "captured_before_final",
        "data_building",
        "data_missing",
        "lake_unavailable",
    )
    codes = {exclusion.code for exclusion in exclusions.values()}
    return next(code for code in order if code in codes)
