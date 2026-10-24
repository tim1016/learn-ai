"""Admitted lake inputs for a research study: the pattern Feature, Signal and later studies share (#3521).

A study that computes from the lake does three things before it computes.

1. **Names its input identity** (:class:`MinuteWindowIdentity`): symbol, data
   kind, adjustment, session policy, window, warmup. The coverage query and the
   compute read exactly that identity — there is no second, looser question.
2. **Admits it** (:func:`admit_minute_inputs`): reads every session once
   through committed-byte admission and the corporate-action receipt check,
   asks the existing materializer (catalog claims, lease coordination) for the
   sessions that are absent or whose file a capture can put right (bytes with
   no committed receipt are a capture in flight or one that died — the
   materializer waits out the one and steals from the other), and reads those
   again. The UI is never a writer.
3. **Refuses, or hands over bars with a receipt.** A required session that is
   missing, being built by another writer, corrupt, captured before its data
   stopped forming, or unavailable from the provider ends in a typed
   :class:`StudyInputRefusal` carrying the counts — never a result computed on a
   shorter series. Success carries the same counts
   plus the admitted snapshot (the sha256 of every byte read), which is what a
   result cache must key on.

Session outcomes are a closed vocabulary (:data:`SessionOutcome`). Coverage
counts are by distinct session, with artifact counts kept apart (a session is
one minute artifact, but the materializer also reports metadata artifacts).

A retained day older than the provider's history floor stays readable; a
missing one is ``unavailable_from_provider`` and is never asked of the provider
(the request would be refused, and a refused day ends a whole backfill).

A day captured before the study's session policy finished forming
(:mod:`app.data_lake.capture_finality`) is held but short: its tail is missing,
and nothing in the lake refreshes a committed day. Admission refuses it as
``captured_before_final`` and asks the vendor for nothing — it neither computes
on that tape nor rewrites the day, which every other reader of the lake shares.
A held early day stays refused: there is no re-capture path (``docs/known-gaps.md``). The lake keeps
one copy of a day for every reader, so a study that has to capture a day starts only a claim lease after the day's
*extended* final instant, whatever session it reads: a capture it makes itself is then one that rule calls final
for every reader, and a writer that claimed before the final instant has published or expired. A regular-hours
study that finds the day held waits only for its own session's final instant plus that lease, and a regular-hours
study that would have to capture it first refuses (``window_not_complete``) rather than write a day an
extended-hours study then refuses for good.

Lead-in. A study that warms up on sessions before its window may let the
lead-in start late (``lead_in="trim_unsupplied_prefix"``): the oldest lead-in
sessions the vendor answered with no bars at all (``provider_no_data``) are
dropped from the front, because a symbol cannot be asked for bars from before it
traded. Nothing else is dropped. A rate limit, an API error, an unknown symbol or
a tape the lake's publication gate rejected says nothing about whether the symbol
traded, so it still refuses, and so does a hole after a session that has bars. The
dropped sessions stay in the counts (``unavailable_from_provider``) and in
:attr:`StudyCoverage.dropped`; the admitted identity carries the lead-in that was
read.
Repeated admissions reuse the catalog's empty-answer evidence for that prefix;
they do not spend its fetch retry budget and turn an accepted shortened lead-in
into a refusal. A subsequently captured file is still read first and wins.

Fold. The lake holds minute bars; a coarser study resolution is folded from
them by :func:`fold_minute_bars` — never by the chart's resampler, which fills
missing minutes forward.

Time: every temporal value that leaves this module is ``int64 ms UTC`` — a
session by its canonical open, a requested window bound by its ET midnight.
Sessions, completeness and the provider floor come from the canonical calendar.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

import asyncpg

from app.data_lake import catalog_client
from app.data_lake.adjustment_versions import AdjustmentVersionError, adjusted_root_for, companion_path
from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.capture_finality import (
    AFTER_HOURS_TAIL_MS,
    CLAIM_LEASE_MS,
    VENDOR_DELAY_MS,
    CaptureSession,
    capture_final_ms,
    captured_early,
)
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.path_policy import LeanMinuteBarPath
from app.data_lake.polygon_fetcher import polygon_history_floor
from app.data_lake.root_identity import read_marker
from app.data_lake.run_materialization import (
    LakeMaterializationError,
    MinuteWindowMaterialization,
    materialize_minute_window,
)
from app.data_lake.types import ArtifactFailure, ArtifactFailureReason, is_lake_addressable_symbol
from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.availability import _DECODE_ERRORS
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.research.sweep.snapshot import DataSnapshot
from app.utils.background_loop import CallerStoppedWaitingError, run_on_background_loop
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from app.utils.timestamps import display_time, now_ms_utc

logger = logging.getLogger(__name__)

#: The lake reader's session filter: ``regular`` is the scheduled session
#: (early closes respected), ``extended`` is every bar the provider served for
#: the day — pre-market and after-hours included.
SessionPolicy = CaptureSession
AdjustmentMode = Literal["raw", "polygon_split_adjusted"]

#: The fold rule a study names in its identity: what a coarser bar is made of (see :func:`fold_minute_bars`).
FOLD_RULE = "minute_fold_et_wall_clock_no_fill"

#: What admission concludes about one required session.
SessionOutcome = Literal["ready", "missing", "building", "corrupt", "captured_before_final", "unavailable_from_provider"]

RefusalCode = Literal[
    "resolution_unsupported",
    "symbol_not_addressable",
    "no_sessions_in_window",
    "window_not_complete",
    "data_unavailable_from_provider",
    "data_corrupt",
    "captured_before_final",
    "data_building",
    "data_missing",
    "lake_unavailable",
    # A study's own: admission read what the policy asked for, and the lead-in is still shorter than its sizing
    # (the provider's history floor, a late listing, sparse sessions), which the study's warmup policy does not allow.
    "warmup_shortened",
]

#: What a study does when the vendor has no bars for its oldest lead-in sessions: ``require_all`` refuses; the other
#: drops the unsupplied prefix and reads the lead-in that exists (see the module docstring).
LeadIn = Literal["require_all", "trim_unsupplied_prefix"]

#: Sessions named in a refusal payload; the total is always reported beside them.
MAX_LISTED_SESSIONS = 25

#: What each failure the materializer can report means for the session it names. Total over the vocabulary, so
#: a new reason cannot reach a session without someone deciding what it is (the test pins the keys).
#:
#: ``unavailable_from_provider``: the vendor refused or could not supply usable bars (``validation_failed`` is the
#: lake's publication gate refusing duplicate or non-increasing timestamps, wrong-day bars or invalid OHLC: nothing
#: corrupt was written). ``building``: another writer holds the session and has not finished. ``missing``: the lake
#: tried and did not capture the session for a reason of its own (storage, an internal error, an aborted run, the
#: calendar disagreeing) — absent, not damaged.
_OUTCOME_FOR_FAILURE: Mapping[ArtifactFailureReason, SessionOutcome] = {
    "provider_auth_error": "unavailable_from_provider",
    "provider_entitlement_error": "unavailable_from_provider",
    "provider_rate_limited": "unavailable_from_provider",
    "provider_api_error": "unavailable_from_provider",
    "provider_no_data": "unavailable_from_provider",
    "unknown_symbol": "unavailable_from_provider",
    "validation_failed": "unavailable_from_provider",
    "lease_timeout": "building",
    "fetch_timeout": "building",
    "io_error": "missing",
    "internal_error": "missing",
    "run_aborted": "missing",
    "launcher_unreachable": "missing",
    "session_not_produced": "missing",
    "unsupported_resolution": "missing",
    "unsupported_artifact_kind": "missing",
    "corp_action_revision_mismatch": "missing",
    "data_contract_mismatch": "missing",
}

# What reading an admitted file can raise when the file, not the process, is at fault: no committed receipt, a
# stale or missing corporate-action record, a damaged archive, an unparseable row, a file the process cannot open.
_UNREADABLE_FILE: tuple[type[Exception], ...] = (*_DECODE_ERRORS, AdjustmentVersionError)

# Of those, the ones a capture puts right: bytes with no committed receipt (a writer still publishing, or one that
# died between the rename and the commit) and an adjustment record the lake rewrites. The materializer arbitrates —
# it waits out a live writer and steals a dead one's lease — so admission asks it and never calls the file damaged.
_REPAIRED_BY_CAPTURE: tuple[type[Exception], ...] = (LakeAdmissionError, AdjustmentVersionError)

#: Which code a study answers with when several sessions (or other inputs) fail for different reasons: the first outcome
#: in this order that any of them shows.
REFUSAL_PRECEDENCE: tuple[tuple[SessionOutcome, RefusalCode], ...] = (
    ("unavailable_from_provider", "data_unavailable_from_provider"),
    ("corrupt", "data_corrupt"),
    ("captured_before_final", "captured_before_final"),
    ("building", "data_building"),
    ("missing", "data_missing"),
)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinuteWindowIdentity:
    """The minute-bar input a study reads: what the compute and the coverage query both name.

    Data kind is Polygon trade bars at one-minute resolution (the lake's
    resolution; a coarser study resolution is a fold of these and belongs to
    the study's own identity). ``warmup_sessions`` are the scheduled sessions
    read before ``window_start``; the window itself is every scheduled session
    in ``[window_start, window_end]``.
    """

    symbol: str
    price_adjustment_mode: AdjustmentMode
    session_policy: SessionPolicy
    window_start: date
    window_end: date
    warmup_sessions: int = 0

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError(f"symbol must be upper-case: {self.symbol!r}")
        if self.window_end < self.window_start:
            raise ValueError(f"window_end {self.window_end} precedes window_start {self.window_start}")
        if self.warmup_sessions < 0:
            raise ValueError("warmup_sessions must not be negative")

    @property
    def adjusted(self) -> bool:
        return self.price_adjustment_mode == "polygon_split_adjusted"

    def as_dict(self) -> dict[str, Any]:
        """The identity as it travels: bounds are ET-midnight ``int64 ms UTC``, never date strings."""
        return {
            "data_kind": "trade_bars",
            "provider": "polygon",
            "base_resolution": "minute",
            "symbol": self.symbol,
            "price_adjustment_mode": self.price_adjustment_mode,
            "session_policy": self.session_policy,
            "window_start_ms": et_midnight_ms(self.window_start),
            "window_end_ms": et_midnight_ms(self.window_end),
            "warmup_sessions": self.warmup_sessions,
        }


# ---------------------------------------------------------------------------
# Coverage and refusal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionIssue:
    """One required session admission could not make ready, and why."""

    group: Literal["window", "warmup"]
    trading_date: date
    outcome: SessionOutcome
    reason: str
    #: The materializer's own reason code for the session (``ArtifactFailure.reason``), when it reported one: a study
    #: that treats some failures differently from others (an empty vendor answer against a rate limit) reads it here
    #: instead of parsing ``reason``.
    failure_reason: ArtifactFailureReason | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "trading_date_ms": session_open_ms_utc(self.trading_date),
            "outcome": self.outcome,
            "reason": self.reason,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class GroupCoverage:
    """Distinct-session counts for the window or the warmup.

    ``reused`` sessions were ready before this admission began; ``added`` were
    made ready by it. ``missing`` are held nowhere and were not fetched (the
    request could not complete anyway, or the fetch produced nothing and said
    nothing). ``failed`` is
    ``building + corrupt + captured_before_final + unavailable_from_provider``.
    """

    expected: int = 0
    reused: int = 0
    added: int = 0
    missing: int = 0
    building: int = 0
    corrupt: int = 0
    captured_before_final: int = 0
    unavailable_from_provider: int = 0

    @property
    def failed(self) -> int:
        return self.building + self.corrupt + self.captured_before_final + self.unavailable_from_provider

    def as_dict(self) -> dict[str, int]:
        return {
            "expected_sessions": self.expected,
            "reused": self.reused,
            "added": self.added,
            "missing": self.missing,
            "failed": self.failed,
            "building": self.building,
            "corrupt": self.corrupt,
            "captured_before_final": self.captured_before_final,
            "unavailable_from_provider": self.unavailable_from_provider,
        }


@dataclass(frozen=True)
class StudyCoverage:
    """What admission did for a study's input: session counts, artifact counts, and every session it could not ready."""

    window: GroupCoverage
    warmup: GroupCoverage
    artifacts_fetched: int = 0
    artifacts_reused: int = 0
    artifacts_refreshed: int = 0
    issues: tuple[SessionIssue, ...] = ()
    #: Lead-in sessions dropped from the front under ``lead_in="trim_unsupplied_prefix"`` (oldest first): the vendor
    #: answered each with no bars. They are still counted in the warmup group (``unavailable_from_provider``, so
    #: ``failed``) and still listed in ``issues``; they are not what stops a run.
    dropped: tuple[date, ...] = ()

    @property
    def blocking(self) -> tuple[SessionIssue, ...]:
        """The sessions that keep the run from going ahead: every issue except the lead-in prefix that was dropped."""
        return tuple(issue for issue in self.issues if issue.trading_date not in self.dropped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.as_dict(),
            "warmup": self.warmup.as_dict(),
            "artifacts": {
                "fetched": self.artifacts_fetched,
                "reused": self.artifacts_reused,
                "refreshed": self.artifacts_refreshed,
            },
        }


class StudyInputRefusal(Exception):
    """A study's required input was not admitted; no result is computed.

    ``code`` is the closed :data:`RefusalCode`, ``reason`` plain words for the
    person who ran the study, ``coverage`` the counts so a data-source line can
    show what admission did before it stopped (``None`` when it never started).
    """

    def __init__(self, code: RefusalCode, reason: str, *, coverage: StudyCoverage | None = None) -> None:
        super().__init__(reason)
        self.code: RefusalCode = code
        self.reason = reason
        self.coverage = coverage

    def as_dict(self) -> dict[str, Any]:
        issues = self.coverage.issues if self.coverage is not None else ()
        return {
            "code": self.code,
            "reason": self.reason,
            "sessions": [issue.as_dict() for issue in issues[:MAX_LISTED_SESSIONS]],
            "sessions_total": len(issues),
            "coverage": self.coverage.as_dict() if self.coverage is not None else None,
        }


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmittedMinuteInputs:
    """Bars a study may compute on, with the receipt of the bytes they came from.

    ``snapshot`` fingerprints every artifact read (and its corporate-action
    record), so ``snapshot.digest()`` changes whenever the lake is repaired or
    refreshed under the same identity — the revision a result cache keys on.
    """

    identity: MinuteWindowIdentity
    warmup_bars: tuple[TradeBar, ...]
    window_bars: tuple[TradeBar, ...]
    snapshot: DataSnapshot
    data_root_id: UUID
    coverage: StudyCoverage

    @property
    def snapshot_digest(self) -> str:
        return self.snapshot.digest()

    @property
    def window_sessions(self) -> tuple[date, ...]:
        """The scheduled sessions of the window itself (the warmup sessions that precede it excluded)."""
        return self.snapshot.sessions[self.identity.warmup_sessions :]


@dataclass
class _SessionRead:
    """What reading one session's file showed; bars and receipts only when ``state == "ready"``.

    ``absent``: no file, or none of its bars belong to the policy's session. ``unreadable``: a file that cannot be
    admitted or decoded. ``invalid``: it decoded to bars that break the session's invariants. ``early``: readable,
    but captured before the policy's data stopped forming, so its tail is missing for good; no capture the lake makes
    on its own puts that right, so it is never ``repairable``. ``repairable`` is whether a capture can put the
    session right (see :data:`_REPAIRED_BY_CAPTURE`). ``uncommitted``: the bytes have no committed receipt, which a
    writer still publishing and a writer that died both leave.
    """

    state: Literal["ready", "absent", "unreadable", "invalid", "early"]
    reason: str = ""
    repairable: bool = False
    uncommitted: bool = False
    bars: list[TradeBar] = field(default_factory=list)
    receipts: dict[str, str] = field(default_factory=dict)


def _no_check() -> None:
    return None


def lead_in_sessions(window_start: date, count: int) -> list[date]:
    """The ``count`` scheduled sessions before ``window_start``, oldest first (canonical calendar)."""
    if count == 0:
        return []
    span_days = 14 + 2 * count
    while span_days <= 20 * 366:
        sessions = expected_sessions(window_start - timedelta(days=span_days), window_start - timedelta(days=1))
        if len(sessions) >= count:
            return sessions[-count:]
        span_days *= 2
    raise ValueError(f"fewer than {count} scheduled sessions exist before {window_start}")


def _read_session(
    root: Path, reader: LeanMinuteDataReader, identity: MinuteWindowIdentity, day: date
) -> _SessionRead:
    """Admit, verify and parse one session's file; the bytes hashed are the bytes parsed."""
    relative = LeanMinuteBarPath(market="usa", symbol=identity.symbol, trading_date=day, data_type="trade").relative_path()
    path = root / relative
    if not path.exists():
        return _SessionRead("absent", "no file in the lake", repairable=True)
    try:
        payload = read_committed_bytes(path)
        receipts = {relative.as_posix(): sha256(payload).hexdigest()}
        companion: bytes | None = None
        if adjusted_root_for(path) is not None:
            companion = companion_path(path).read_bytes()
            receipts[companion_path(relative).as_posix()] = sha256(companion).hexdigest()
        reader.adjustment_guard.verify(path, payload, identity.symbol, companion_payload=companion)
        bars = reader.parse_day_zip(payload, identity.symbol, day)
    except _UNREADABLE_FILE as exc:
        return _SessionRead(
            "unreadable",
            f"{type(exc).__name__}: {exc}",
            repairable=isinstance(exc, _REPAIRED_BY_CAPTURE),
            uncommitted=isinstance(exc, LakeAdmissionError),
        )
    if not bars:
        return _SessionRead("absent", f"the file holds no {identity.session_policy}-session bars", repairable=True)
    previous: int | None = None
    for bar in bars:
        if et_date_at_ms(bar.start_ms) != day:
            return _SessionRead("invalid", f"a bar at {bar.start_ms} ms lies outside the session date {day}")
        if previous is not None and bar.start_ms <= previous:
            return _SessionRead(
                "invalid", f"timestamps are not strictly increasing at {bar.start_ms} ms (after {previous} ms)"
            )
        previous = bar.start_ms
    return _SessionRead("ready", bars=bars, receipts=receipts)


def _early_captures(
    *, symbol: str, days: Sequence[date], price_adjustment_mode: AdjustmentMode, session: SessionPolicy
) -> dict[date, int]:
    """The days among ``days`` the lake holds a minute artifact for that may have been captured before ``session`` data
    was final, each with the catalog's completion time (``int64 ms UTC``).

    Judged by :func:`~app.data_lake.capture_finality.captured_early`, from the time the catalog recorded, never from
    the file. Blocking; raises :class:`CatalogUnavailableError` when the catalog cannot answer.
    """
    if not days:
        return {}

    async def _completion_times() -> dict[date, int]:
        await catalog_client.init_pool()
        return await catalog_client.select_minute_capture_times(
            "usa", symbol, "trade", min(days), max(days), price_adjustment_mode=price_adjustment_mode
        )

    try:
        completed = run_on_background_loop(_completion_times(), timeout=35)
    except (asyncpg.PostgresError, OSError, TimeoutError, CallerStoppedWaitingError) as exc:
        raise CatalogUnavailableError(f"cannot read capture times from the catalog ({type(exc).__name__})") from exc
    return {day: completed[day] for day in days if day in completed and captured_early(completed[day], day, session)}


def _describe_failure(failure: ArtifactFailure) -> str:
    return f"{failure.reason}" + (f": {failure.detail}" if failure.detail else "")


def _recorded_empty_prefix(
    identity: MinuteWindowIdentity, lead: Sequence[date], reads: Mapping[date, _SessionRead]
) -> dict[date, ArtifactFailure]:
    """Reuse only failed empty-answer evidence at the front of an absent lead-in."""
    if not lead or reads[lead[0]].state != "absent":
        return {}

    async def _coverage() -> list[catalog_client.ArtifactCoverageRow]:
        await catalog_client.init_pool()
        return await catalog_client.select_artifact_coverage(
            market="usa", symbol=identity.symbol, data_type="trade", provider="polygon",
            price_adjustment_mode=identity.price_adjustment_mode,
            start_trading_date=lead[0], end_trading_date=lead[-1],
        )

    try:
        rows = run_on_background_loop(_coverage(), timeout=35)
    except (asyncpg.PostgresError, OSError, TimeoutError, CallerStoppedWaitingError) as exc:
        raise CatalogUnavailableError(f"cannot read lead-in evidence from the catalog ({type(exc).__name__})") from exc
    by_day = {row.trading_date: row for row in rows}
    failures: dict[date, ArtifactFailure] = {}
    for day in lead:
        row = by_day.get(day)
        if reads[day].state != "absent" or row is None or row.status != "failed" or row.last_error != "provider_no_data":
            break
        failures[day] = ArtifactFailure(
            artifact_kind="time_series_bars", symbol=identity.symbol, trading_date=day,
            data_type="trade", reason="provider_no_data",
        )
    return failures


class IssueLike(Protocol):
    """What :func:`summarise_issues` reads of an input a study could not have: its outcome, the session, and plain words."""

    @property
    def outcome(self) -> str: ...

    @property
    def trading_date(self) -> date: ...

    @property
    def reason(self) -> str: ...


def summarise_issues(issues: Sequence[IssueLike], total: int, *, unit: str = "sessions") -> str:
    """One plain sentence per outcome, naming up to three inputs each; ``unit`` is what ``total`` counts."""
    by_outcome: dict[str, list[IssueLike]] = {}
    for issue in issues:
        by_outcome.setdefault(issue.outcome, []).append(issue)
    parts: list[str] = []
    meaning: Mapping[str, str] = {
        "unavailable_from_provider": "cannot be supplied by the provider",
        "corrupt": "are in the lake but cannot be read",
        "captured_before_final": "were captured before their data stopped forming, so their tail is missing",
        "building": "are still being captured by another run",
        "missing": "are not in the lake",
        "window_not_complete": "are not over in New York yet, so the lake takes none of their data",
        "symbol_not_addressable": "name something the lake cannot store",
        "lake_unavailable": "could not be asked of the lake, whose catalog is unavailable",
    }
    for outcome, listed in by_outcome.items():
        shown = ", ".join(f"{i.trading_date.isoformat()} ({i.reason})" for i in listed[:3])
        more = f" and {len(listed) - 3} more" if len(listed) > 3 else ""
        parts.append(f"{len(listed)} of {total} {unit} {meaning[outcome]}: {shown}{more}")
    return "; ".join(parts)


def _why_not_ready(
    read: _SessionRead,
    *,
    failure: ArtifactFailure | None,
    older_than_floor: bool,
    capture_timed_out: bool,
    floor: date,
    fetched: bool,
) -> tuple[SessionOutcome, str]:
    """The outcome and plain reason for a session the read pass could not admit.

    A session captured before its data stopped forming is refused as such
    whatever else is true: it is held, readable and short, and no capture asked
    for it. Otherwise the materializer's own failure for the session wins when it
    has one — it says why the lake could not repair or fill it. A file that
    exists but cannot be read is corrupt whatever else is true, unless a capture
    was asked to put it right and is still at it, or no capture was asked and the
    file is only missing its receipt (a writer mid-publish looks the same as one
    that died; nothing was done to tell them apart, so it is not called damaged).
    A missing session older than the provider's history was never asked of the
    provider.
    """
    if read.state == "early":
        return "captured_before_final", read.reason
    if failure is not None:
        reason = _describe_failure(failure)
        if read.state != "absent":
            reason = f"{read.reason}; rebuild: {reason}"
        return _OUTCOME_FOR_FAILURE[failure.reason], reason
    if read.state in ("unreadable", "invalid"):
        if capture_timed_out and read.repairable:
            return "building", f"{read.reason}; the capture did not finish in time"
        if read.uncommitted and not fetched:
            return "building", f"{read.reason}; no capture was asked, so a writer still publishing it is not ruled out"
        return "corrupt", read.reason
    if older_than_floor:
        return "unavailable_from_provider", f"older than the provider's history, which starts {floor.isoformat()}"
    if capture_timed_out:
        return "building", "the capture did not finish in time"
    return "missing", read.reason if fetched else "not in the lake; not fetched"


def _group_coverage(
    days: Sequence[date],
    final: Mapping[date, SessionOutcome],
    first_read: Mapping[date, _SessionRead],
) -> GroupCoverage:
    counts: Counter[str] = Counter()
    for day in days:
        outcome = final[day]
        if outcome == "ready":
            counts["reused" if first_read[day].state == "ready" else "added"] += 1
        else:
            counts[outcome] += 1
    return GroupCoverage(
        expected=len(days),
        reused=counts["reused"],
        added=counts["added"],
        missing=counts["missing"],
        building=counts["building"],
        corrupt=counts["corrupt"],
        captured_before_final=counts["captured_before_final"],
        unavailable_from_provider=counts["unavailable_from_provider"],
    )


def admit_minute_inputs(
    identity: MinuteWindowIdentity,
    *,
    requester: str,
    now_ms: int | None = None,
    check_cancelled: Callable[[], None] = _no_check,
    lead_in: LeadIn = "require_all",
) -> AdmittedMinuteInputs:
    """Read every session once, have the materializer capture what the lake lacks, or refuse with counts.

    Each required session's file is read, committed-byte admitted and parsed
    exactly once; only a session that was not ready is read again, after the
    materializer (catalog claims, lease coordination) has had its say. (An
    adjusted window is read again whole after a capture: the capture can
    rebuild days that were ready, and one series must sit on one basis.) What a
    session turns out to be comes from that read and from the materializer's
    own failure for it — never from a guess: a file with no committed receipt
    is a capture in flight or one that died, and the materializer decides which.

    Blocking: call from a worker thread (materialization and byte admission
    cross to the shared background loop). ``now_ms`` fixes the clock for the
    completed-session check and the provider floor; production passes none.
    ``check_cancelled`` is called between the steps that can be abandoned and
    raises to abandon the study. ``lead_in`` says what to do when the vendor has
    no bars for the oldest lead-in sessions: refuse (the default), or drop that
    prefix and admit the lead-in that exists (see the module docstring); the
    identity on the result is then the lead-in actually read.
    """
    now = now_ms_utc() if now_ms is None else now_ms
    symbol = identity.symbol
    if not is_lake_addressable_symbol(symbol):
        raise StudyInputRefusal(
            "symbol_not_addressable", f"{symbol} cannot be held in the data lake (tickers start with a letter and use only letters, digits and dots)."
        )
    window = expected_sessions(identity.window_start, identity.window_end)
    if not window:
        raise StudyInputRefusal(
            "no_sessions_in_window",
            f"{identity.window_start.isoformat()} to {identity.window_end.isoformat()} holds no trading session.",
        )
    lead = lead_in_sessions(window[0], identity.warmup_sessions)
    required = [*lead, *window]
    # A capture counts as final only from its completion less the claim lease (``captured_early``). A study that
    # starts a lease after the final instant sees every live claim taken after it (none is renewed, and an expired
    # one cannot publish). This is the gate for reading; a study that has to capture a day waits for the extended
    # instant too (below), so every capture it makes itself is final for every reader.
    live = [day for day in required if now < capture_final_ms(day, identity.session_policy) + CLAIM_LEASE_MS]
    if live:
        raise StudyInputRefusal(
            "window_not_complete",
            f"{len(live)} session(s) have not finished forming (from {live[0].isoformat()}); the lake holds a session "
            f"only once its {identity.session_policy} data is final (the close, plus the after-hours tape for "
            "extended, plus the vendor's 15-minute delay, plus the "
            f"{CLAIM_LEASE_MS // 60_000} minutes a capture begun before then may still be publishing). End the window "
            "on an earlier session.",
        )

    roots = resolve_data_roots(source="polygon", adjusted=identity.adjusted)
    root = roots[0]
    reader = LeanMinuteDataReader(roots, session=identity.session_policy)
    floor = polygon_history_floor(et_date_at_ms(now))

    first_read: dict[date, _SessionRead] = {}
    try:
        for day in required:
            first_read[day] = _read_session(root, reader, identity, day)
            check_cancelled()
    except CatalogUnavailableError as exc:
        raise StudyInputRefusal("lake_unavailable", f"The data lake's catalog is unavailable ({exc}).") from exc

    try:
        early = _early_captures(
            symbol=symbol,
            days=[day for day in required if first_read[day].state == "ready"],
            price_adjustment_mode=identity.price_adjustment_mode,
            session=identity.session_policy,
        )
    except CatalogUnavailableError as exc:
        raise StudyInputRefusal("lake_unavailable", f"The data lake's catalog is unavailable ({exc}).") from exc
    for day, completed_ms in early.items():
        first_read[day] = _SessionRead(
            "early",
            f"completed at {display_time(completed_ms)} ET; the vendor may have been asked as early as "
            f"{display_time(completed_ms - CLAIM_LEASE_MS)} ET, before its {identity.session_policy} session had "
            f"closed and been delivered ({display_time(capture_final_ms(day, identity.session_policy))} ET); it must "
            f"be captured again before a study of the {identity.session_policy} session can use it",
        )
    check_cancelled()

    try:
        failures = _recorded_empty_prefix(identity, lead, first_read) if lead_in == "trim_unsupplied_prefix" else {}
    except CatalogUnavailableError as exc:
        raise StudyInputRefusal("lake_unavailable", f"The data lake's catalog is unavailable ({exc}).") from exc
    needs_work = [day for day in required if first_read[day].state != "ready" and day not in failures]
    fixable = [day for day in needs_work if day >= floor and first_read[day].repairable]
    materialization: MinuteWindowMaterialization | None = None
    if fixable and len(fixable) == len(needs_work):
        # Only a request the lake can complete is worth the provider's calls. A capture writes the day for every
        # reader, so whatever session this study reads it waits for the extended tape too: a regular-hours capture
        # taken in the hours after the close would read as final to the next extended-hours study and be refused.
        unsettled = [day for day in fixable if now < capture_final_ms(day, "extended") + CLAIM_LEASE_MS]
        if unsettled:
            raise StudyInputRefusal(
                "window_not_complete",
                f"{len(unsettled)} session(s) the lake would have to capture have not finished forming for every "
                f"reader (from {unsettled[0].isoformat()}): the lake keeps one copy of a day, which an extended-hours "
                "study reads whole, so a day is captured only once its after-hours tape is final too (the close, plus "
                f"{AFTER_HOURS_TAIL_MS // 3_600_000} hours of after-hours, plus the vendor's "
                f"{VENDOR_DELAY_MS // 60_000}-minute delay, plus the "
                f"{CLAIM_LEASE_MS // 60_000} minutes a capture begun before then may still be publishing). End the "
                "window on an earlier session, or run it later.",
            )
        try:
            materialization = materialize_minute_window(
                symbol=symbol,
                start=min(fixable),
                end=max(fixable),
                price_adjustment_mode=identity.price_adjustment_mode,
                requester=requester,
            )
        except LakeMaterializationError as exc:
            raise StudyInputRefusal("lake_unavailable", f"The data lake cannot materialize this window: {exc}") from exc
        if materialization.interruption == "catalog_unavailable":
            raise StudyInputRefusal(
                "lake_unavailable", f"The data lake's catalog is unavailable ({materialization.detail})."
            )
    check_cancelled()

    reads = dict(first_read)
    if materialization is not None:
        failures.update({f.trading_date: f for f in materialization.withheld if f.trading_date is not None})
        # A capture of an adjusted symbol can move the basis of every day the lake holds for it, including days that
        # read ready before it. The reader pins the first basis it sees, so the days read again are read with a reader
        # of their own, and every required day is read again: one read on the old basis and one on the new would
        # otherwise be refused as mixed, or worse, admitted as one series.
        again = required if identity.adjusted else fixable
        if identity.adjusted:
            reader = LeanMinuteDataReader(roots, session=identity.session_policy)
        try:
            for day in again:
                reads[day] = _read_session(root, reader, identity, day)
                check_cancelled()
        except CatalogUnavailableError as exc:
            raise StudyInputRefusal("lake_unavailable", f"The data lake's catalog is unavailable ({exc}).") from exc

    final: dict[date, SessionOutcome] = {}
    reasons: dict[date, str] = {}
    for day in required:
        if reads[day].state == "ready":
            final[day] = "ready"
        else:
            final[day], reasons[day] = _why_not_ready(
                reads[day],
                failure=failures.get(day),
                older_than_floor=day < floor and first_read[day].state == "absent",
                capture_timed_out=materialization is not None and materialization.interruption == "did_not_finish",
                floor=floor,
                fetched=materialization is not None,
            )

    dropped = _unsupplied_prefix(lead, final, failures) if lead_in == "trim_unsupplied_prefix" else []
    issues = tuple(
        SessionIssue(
            "warmup" if day in lead else "window",
            day,
            final[day],
            f"{reasons[day]}; dropped from the start of the lead-in" if day in dropped else reasons[day],
            failure_reason=failures[day].reason if day in failures else None,
        )
        for day in required
        if final[day] != "ready"
    )
    coverage = StudyCoverage(
        window=_group_coverage(window, final, first_read),
        warmup=_group_coverage(lead, final, first_read),
        artifacts_fetched=materialization.fetched_artifact_count if materialization else 0,
        artifacts_reused=materialization.reused_artifact_count if materialization else 0,
        artifacts_refreshed=materialization.refreshed_artifact_count if materialization else 0,
        issues=issues,
        dropped=tuple(dropped),
    )
    if coverage.blocking:
        outcomes = {issue.outcome for issue in coverage.blocking}
        code: RefusalCode = next(refusal for outcome, refusal in REFUSAL_PRECEDENCE if outcome in outcomes)
        raise StudyInputRefusal(
            code,
            f"The lake cannot admit this window: {summarise_issues(coverage.blocking, len(required) - len(dropped))}.",
            coverage=coverage,
        )

    kept = lead[len(dropped) :]
    admitted_days = [*kept, *window]
    receipts: dict[str, str] = {}
    for day in admitted_days:
        receipts.update(reads[day].receipts)
    # An admitted managed file proves the root has an identity marker (admission refuses one without).
    marker = read_marker(root.parent.parent)
    if marker is None:
        raise StudyInputRefusal("lake_unavailable", "The data lake root carries no identity, so its bytes cannot be receipted.")
    snapshot = DataSnapshot(
        symbol=symbol,
        resolution="minute",
        data_start=admitted_days[0],
        data_end=admitted_days[-1],
        sessions=tuple(admitted_days),
        artifacts=receipts,
        corporate_action_versions=dict(reader.adjustment_guard.versions),
    )
    return AdmittedMinuteInputs(
        identity=replace(identity, warmup_sessions=len(kept)) if dropped else identity,
        warmup_bars=tuple(bar for day in kept for bar in reads[day].bars),
        window_bars=tuple(bar for day in window for bar in reads[day].bars),
        snapshot=snapshot,
        data_root_id=marker.data_root_id,
        coverage=coverage,
    )


def _unsupplied_prefix(
    lead: Sequence[date], final: Mapping[date, SessionOutcome], failures: Mapping[date, ArtifactFailure]
) -> list[date]:
    """The oldest lead-in sessions the vendor answered with no bars at all: an empty answer, never a failure.

    ``provider_no_data`` is the materializer's own word for it. A rate limit, an API or auth error, an unknown symbol
    or a tape the lake's publication gate rejected (``validation_failed``) says nothing about whether the symbol traded
    that day, so none of them is ever read as 'the history starts here'. The prefix ends at the first session that is
    not one, so a hole after a session with bars is a gap and stays a refusal.
    """
    prefix: list[date] = []
    for day in lead:
        failure = failures.get(day)
        if final[day] != "unavailable_from_provider" or failure is None or failure.reason != "provider_no_data":
            break
        prefix.append(day)
    return prefix


# ---------------------------------------------------------------------------
# Fold
# ---------------------------------------------------------------------------


def fold_minute_bars(bars: Iterable[TradeBar], bar_minutes: int) -> list[TradeBar]:
    """Fold minute bars into ``bar_minutes``-minute bars aligned to the New York wall clock.

    Formula: bucket start = floor(minute start, N minutes) on the ET wall
    clock; open = the first minute's open, high = max, low = min, close = the
    last minute's close, volume = sum, over the minutes the bucket holds. A
    bucket holding no minute yields no bar — nothing is filled, and a bucket
    holding only some of its minutes still yields one (as a vendor aggregate
    does), so the trailing bucket is flushed.
    Not ``engine_bars_service.consolidate_bars``: that is the engine's end-of-data
      flush, ``scan(last.end_ms)``, which leaves a partial trailing bucket
      unemitted; a study folds to what a vendor aggregate holds, so it flushes
      past the trailing bucket's end instead.
    Reference: LEAN Common/Data/Consolidators/TradeBarConsolidator.cs, vendored
      at references/lean/7986ed0aade3ae5de06121682409f05984e32ff7 and ported
      in the engine's consolidator.
    Canonical implementation: app/engine/consolidators/trade_bar_consolidator.py
      (this function drives it and flushes the trailing bucket). ``FOLD_RULE``
      is the name a study's identity gives this rule.
    Validated against: tests/research/test_study_inputs.py::test_fold_matches_an_independent_pandas_reference
    """
    series = list(bars)
    if bar_minutes == 1:
        return series
    if bar_minutes < 1:
        raise ValueError("bar_minutes must be a positive whole number of minutes")
    consolidator = TradeBarConsolidator(timedelta(minutes=bar_minutes))
    folded: list[TradeBar] = []
    for bar in series:
        fired = consolidator.update(bar)
        if fired is not None:
            folded.append(fired)
    if series:
        tail = consolidator.scan(series[-1].start_ms + bar_minutes * 60_000)
        if tail is not None:
            folded.append(tail)
    return folded


def bars_as_rows(bars: Iterable[TradeBar]) -> list[dict[str, float | int]]:
    """Study bar rows: ``timestamp`` is the bar start (``int64 ms UTC``), prices and volume are float64.

    A price on the lake's 1/10,000 grid converts to the float nearest it, so a
    row equals the vendor's float for any vendor price already on that grid.
    """
    return [
        {
            "timestamp": bar.start_ms,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume) if isinstance(bar.volume, Decimal) else float(int(bar.volume)),
        }
        for bar in bars
    ]
