"""Admitted lake stock bars for the cross-sectional study (#3524; PRD #3490 R12-R15, R25, T05, T09, T18, D02).

The study's input contract, field by field and before/after, is the docstring of
:mod:`app.services.cross_sectional_study_service`. This module is the lake side of it.

* **Identity** (:class:`CrossSectionalIdentity`): the universe, data kind, adjustment, session policy, window and
  fold rule; each member's minute-bar input is :meth:`CrossSectionalIdentity.member`. The coverage query and the
  compute read exactly that identity.
* **Admission** (:func:`admit_universe`): each member through :func:`app.research.study_inputs.admit_minute_inputs` —
  the existing materializer (catalog claims, lease coordination) and committed-byte admission. The UI is never a writer.
* **Daily rows** (:func:`daily_rows`): the lake holds minute bars, so a daily bar is a fold of the admitted minutes of
  one New York session, by the lake's own reduction (:func:`app.data_lake.derived_daily.aggregate_minute_to_daily`) —
  not the whole-symbol daily rollup. The rollup is rebuilt whenever a symbol's catalogued coverage changes, offers no
  session choice, and is not part of a per-session receipt. Under the ``extended`` policy the study's rows equal the
  rollup's rows (``tests/research/test_cross_sectional_inputs.py``); under ``regular`` they fold the scheduled session.
* **Partial universes** (T18): a member the lake cannot admit is a :class:`MemberExclusion` with its code, reason and
  coverage, not a dropped symbol. When no member can be admitted the study refuses.

Time: every temporal value leaving this module is ``int64 ms UTC``; a daily row is stamped with its session's open.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.data_lake.derived_daily import aggregate_minute_to_daily
from app.data_lake.lean_writer import MinuteTradeBar
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import session_open_ms_utc
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
from app.utils.session_anchors import et_midnight_ms

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

#: The fold rule named in the identity: what a daily bar is made of.
DAILY_FOLD_RULE = "minute_fold_per_et_session_no_fill"

#: What a daily row's ``timestamp`` is.
BAR_TIMESTAMP_ANCHOR = "session_open"

#: Where a member can be left out of the statistics.
ExclusionStage = Literal["stock_bars", "iv_inputs", "iv_diagnostics", "computation"]

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
    Validated against: tests/research/test_cross_sectional_inputs.py::test_daily_rows_equal_the_lakes_own_daily_rollup
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


@dataclass(frozen=True)
class MemberExclusion:
    """A member left out of the statistics, with where, why and whether asking again could answer differently."""

    ticker: str
    stage: ExclusionStage
    code: str
    reason: str
    retryable: bool
    refusal: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "stage": self.stage,
            "code": self.code,
            "reason": self.reason,
            "retryable": self.retryable,
            "refusal": self.refusal,
        }


def exclusion_from_refusal(ticker: str, refusal: StudyInputRefusal) -> MemberExclusion:
    return MemberExclusion(
        ticker=ticker,
        stage="stock_bars",
        code=refusal.code,
        reason=refusal.reason,
        retryable=refusal.code not in _STRUCTURAL_CODES,
        refusal=refusal.as_dict(),
    )


@dataclass(frozen=True)
class AdmittedMember:
    """One member's admitted stock bars: the daily rows to compute on, and the receipt of the bytes they came from."""

    ticker: str
    admitted: AdmittedMinuteInputs
    rows: list[dict[str, float | int]]
    identity_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "status": "admitted",
            "identity_digest": self.identity_digest,
            "snapshot_digest": self.admitted.snapshot_digest,
            "data_root_id": str(self.admitted.data_root_id),
            "corporate_action_versions": dict(self.admitted.snapshot.corporate_action_versions),
            "coverage": self.admitted.coverage.as_dict(),
            "minute_bars_read": len(self.admitted.window_bars),
            "daily_bars": len(self.rows),
        }


@dataclass
class UniverseAdmission:
    """What admission did for every requested member, in request order."""

    members: dict[str, AdmittedMember] = field(default_factory=dict)
    excluded: dict[str, MemberExclusion] = field(default_factory=dict)
    #: Coverage of every member admission started on, admitted or refused, for the data-source line.
    coverage: dict[str, StudyCoverage] = field(default_factory=dict)

    def stock_bars(self) -> dict[str, list[dict[str, float | int]]]:
        return {ticker: member.rows for ticker, member in self.members.items()}


def admit_universe(
    identity: CrossSectionalIdentity,
    *,
    requester: str,
    now_ms: int | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    on_member: Callable[[int, int, str, str], None] = lambda index, total, ticker, outcome: None,
) -> UniverseAdmission:
    """Admit each member's stock bars in turn; a member the lake cannot admit is excluded with the reason, not dropped.

    Blocking: call from a worker thread. Members are admitted one after another — the materializer coordinates
    concurrent writers and the provider's budget, and a cold universe is many sessions per member. A cancellation
    raised by ``check_cancelled`` propagates; only a typed refusal excludes a member.
    """
    admission = UniverseAdmission()
    total = len(identity.symbols)
    for index, ticker in enumerate(identity.symbols):
        check_cancelled()
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
        admission.members[ticker] = AdmittedMember(
            ticker=ticker,
            admitted=admitted,
            rows=daily_rows(admitted.window_bars),
            identity_digest=hash_payload(member_identity.as_dict()),
        )
        admission.coverage[ticker] = admitted.coverage
        on_member(index, total, ticker, "admitted")
    return admission


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
        unavailable_from_provider=sum(g.unavailable_from_provider for g in groups),
    )


def stock_bars_block(identity: CrossSectionalIdentity, admission: UniverseAdmission) -> dict[str, Any]:
    """The data-source entry for the stock bars: the lake, the identity read, and what admission did per member.

    ``coverage`` adds the members' counts: each expected unit is one member-session. ``universe_snapshot_digest``
    is the digest of the admitted members' snapshot digests, which changes when any one member's bytes do.
    """
    coverage = admission.coverage
    members: list[dict[str, Any]] = []
    for ticker in identity.symbols:
        if ticker in admission.members:
            members.append(admission.members[ticker].as_dict())
        else:
            excluded = admission.excluded[ticker]
            held = coverage.get(ticker)
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
    snapshots = sorted((ticker, member.admitted.snapshot_digest) for ticker, member in admission.members.items())
    return {
        "kind": "lake",
        **identity.as_dict(),
        "identity_digest": identity.digest(),
        "universe_snapshot_digest": hash_payload(snapshots) if snapshots else None,
        "coverage": {
            "window": sum_groups(c.window for c in coverage.values()).as_dict(),
            "warmup": sum_groups(c.warmup for c in coverage.values()).as_dict(),
            "artifacts": {
                "fetched": sum(c.artifacts_fetched for c in coverage.values()),
                "reused": sum(c.artifacts_reused for c in coverage.values()),
                "refreshed": sum(c.artifacts_refreshed for c in coverage.values()),
            },
        },
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
        "data_building",
        "data_missing",
        "lake_unavailable",
    )
    codes = {exclusion.code for exclusion in exclusions.values()}
    return next(code for code in order if code in codes)
