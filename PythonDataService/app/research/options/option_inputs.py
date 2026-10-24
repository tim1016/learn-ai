"""Option inputs of the Cross-sectional study, read from the data lake (#3540; ADR 0078).

The study used to ask the vendor, live, for a month's expiries, an expiry's contracts and each contract's daily bars, and
could not say afterwards what it had been told. Now the run saves what it needs into the lake first and reads it back, so
what it computed from is a set of stored files with digests:

* **A listing as of a date** (``option_listing``): every contract alive that date. The expiries of a month are those of
  the listing as of the month's first session (or of a later session when that list lacked a near or a far expiry), and
  an expiry's contracts are those of the listing as of the expiry's first use in the run. Standard contracts only, no cap
  on how many.
* **A contract's minute bars for a session** (``option_bars``, or the session container the nightly archive holds for SPY,
  QQQ and IWM). The price of a contract on a session is :func:`regular_session_close`.
* **A session the lake does not hold** is captured now through :class:`~app.options_backtest.fetching.LakeOptionCapture`
  (the lake's own writers, the process's vendor gate), unless it is older than the options plan, when it is refused and the
  vendor is not asked (the lake already holds what it holds; ``option_history_floor``). The archive adopts what is captured.

:func:`admit_member_option_inputs` does that for one member and returns :class:`MemberOptionInputs`, or raises
:class:`OptionInputRefusal` with every input it could not supply, typed. Nothing is computed on part of the inputs: a member
with one missing option input is left out of the statistics, with the reason.

The risk-free rate is not in the lake. It stays a live reference input from FRED and is counted by :class:`RateTally`, apart
from the lake inputs, with a digest of the values used.

What a capture cannot be, for an option input: ``captured_before_final``. Every option writer refuses before New York
midnight after the session, before any claim, so a stored option file is never a partial session. The study refuses a
window whose last session is not yet that old (``window_not_complete``) before it touches the lake.

Time: every temporal value leaving this module is ``int64 ms UTC``; a session is named by its canonical open.
"""

from __future__ import annotations

import logging
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol

import pandas as pd

from app.config import active_root_id
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import (
    OptionBar,
    OptionBarsFileError,
    OptionDayBars,
    option_minute_trade_dch,
    read_option_day_bars,
)
from app.data_lake.option_lake import (
    OPTION_PRICE_ADJUSTMENT_MODE,
    OptionCaptureResult,
    OptionDataFailure,
    option_history_floor,
    option_read_root,
)
from app.data_lake.option_listing import (
    OPTION_LISTING_FILE_FORMAT,
    OptionListing,
    OptionListingFileError,
    read_option_listing,
)
from app.data_lake.root_identity import read_marker
from app.lean_sidecar.trading_calendar import session_open_ms_utc, session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.data_check import lake_reason_words
from app.options_backtest.fetching import TOOK_TOO_LONG, DownloadFailed, DownloadStopped, LakeOptionCapture
from app.options_backtest.lake_data import OptionBarsCannotBePlaced
from app.research.options.contract_finder import CONTRACT_COLUMNS, find_bracket_contracts
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import (
    MAX_LISTED_SESSIONS,
    REFUSAL_PRECEDENCE,
    RefusalCode,
    SessionOutcome,
    StudyInputRefusal,
    summarise_issues,
)
from app.services.fred_service import RATE_SOURCE_DEFAULT, RATE_SOURCE_FRED, RateResolution
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

#: How the option inputs are obtained, named in the cache key and the data-source block.
OPTION_INPUT_POLICY = "lake_option_inputs/v1"

#: The rule that prices a contract on a session, named in the block: ``regular_session_close``.
PRICE_RULE = "last_regular_hours_trade"
PRICE_RULE_WORDS = (
    "each option is priced at its last regular-hours trade: the close of the latest minute bar that lies wholly inside "
    "the scheduled session, kept only with at least 50 contracts traded in regular hours"
)

_MS_PER_MINUTE = 60_000
_UNDERLYING_SOURCE = "raw_regular_close"
_CONTRACTS_RULE = "standard_only"

#: Issues named in a payload; the total is always reported beside them.
MAX_LISTED_ISSUES = MAX_LISTED_SESSIONS

IssueKind = Literal["listing", "contract_day"]
#: What an option input turned out to be. The session outcomes the stock admission uses, and three the lake can answer
#: for an option input that a stock session cannot be refused for.
IssueOutcome = SessionOutcome | Literal["window_not_complete", "symbol_not_addressable", "lake_unavailable"]


# ---------------------------------------------------------------------------
# What the lake answers, and what each answer means for a member
# ---------------------------------------------------------------------------

#: What each reason the lake gives for an option input it did not bring in means for the member, and whether asking
#: again could change the answer. Total over :data:`~app.data_lake.option_lake.OptionFailureReason`, so a new reason
#: cannot reach a member without someone deciding what it is (a test pins the keys with ``typing.get_args``).
#:
#: ``unavailable_from_provider``: the vendor refused or could not supply usable data (``outside_data_plan`` and
#: ``unknown_symbol`` never change by asking again; a rejected key, an entitlement or a rate limit can, once the owner or
#: time has acted). ``building``: another writer holds the row, or the lake is paused. ``missing``: the lake tried and did
#: not capture it for a reason of its own; a contract that is not in that date's listing stays so.
FAILURE_VERDICT: Mapping[str, tuple[IssueOutcome, bool]] = {
    "provider_auth_error": ("unavailable_from_provider", True),
    "provider_entitlement_error": ("unavailable_from_provider", True),
    "provider_rate_limited": ("unavailable_from_provider", True),
    "provider_api_error": ("unavailable_from_provider", True),
    "provider_no_data": ("unavailable_from_provider", True),
    "validation_failed": ("unavailable_from_provider", True),
    "outside_data_plan": ("unavailable_from_provider", False),
    "unknown_symbol": ("unavailable_from_provider", False),
    "lease_timeout": ("building", True),
    "fetch_timeout": ("building", True),
    "staging_capacity": ("building", True),
    "paused": ("building", True),
    "io_error": ("missing", True),
    "internal_error": ("missing", True),
    "run_aborted": ("missing", True),
    "launcher_unreachable": ("missing", True),
    "session_not_produced": ("missing", True),
    "unsupported_resolution": ("missing", True),
    "unsupported_artifact_kind": ("missing", True),
    "corp_action_revision_mismatch": ("missing", True),
    "data_contract_mismatch": ("missing", True),
    "listing_not_captured": ("missing", True),
    "not_listed": ("missing", False),
    "session_not_over": ("window_not_complete", False),
    "not_a_session": ("window_not_complete", False),
    "symbol_too_long": ("symbol_not_addressable", False),
}

#: The refusal code of each issue outcome; the order is which code a member with several outcomes answers with.
_CODE_ORDER: tuple[tuple[IssueOutcome, RefusalCode], ...] = (
    ("window_not_complete", "window_not_complete"),
    ("symbol_not_addressable", "symbol_not_addressable"),
    *REFUSAL_PRECEDENCE,
    ("lake_unavailable", "lake_unavailable"),
)


@dataclass(frozen=True)
class OptionInputIssue:
    """One option input the lake could not supply, and what that means for the member.

    ``subject`` is the contract's symbol for a contract day, the underlying for a listing. ``failure_reason`` is the
    lake's own reason code when it gave one, and ``reason`` plain words built from it (never the lake's text, which can
    hold a vendor's JSON or a path).
    """

    kind: IssueKind
    trading_date: date
    subject: str
    outcome: IssueOutcome
    reason: str
    failure_reason: str | None
    retryable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "trading_date_ms": session_open_ms_utc(self.trading_date),
            "subject": self.subject,
            "outcome": self.outcome,
            "reason": self.reason,
            "failure_reason": self.failure_reason,
            "retryable": self.retryable,
        }


def _issue_from_failure(kind: IssueKind, day: date, subject: str, failure: OptionDataFailure) -> OptionInputIssue:
    outcome, retryable = FAILURE_VERDICT[failure.reason]
    return OptionInputIssue(
        kind, day, subject, outcome, f"{subject}: {lake_reason_words(failure.reason)}", failure.reason, retryable
    )


def _issue_from_stored_file(kind: IssueKind, day: date, subject: str, error: Exception) -> OptionInputIssue:
    """A stored file the lake could not hand over: not admitted yet (a writer may still be publishing it), or damaged."""
    if isinstance(error, CatalogUnavailableError):
        return OptionInputIssue(
            kind, day, subject, "lake_unavailable", f"{subject}: the data lake's catalog could not be reached",
            None, True,
        )
    if isinstance(error, LakeAdmissionError):
        return OptionInputIssue(
            kind, day, subject, "building",
            f"{subject}: its stored file is not admitted by the catalog, so a writer may still be publishing it",
            "lease_timeout", True,
        )
    return OptionInputIssue(
        kind, day, subject, "corrupt",
        f"{subject}: its stored file cannot be read ({type(error).__name__}), so nothing is read from the lake in its place",
        None, True,
    )


def _issue_not_there_after_capture(kind: IssueKind, day: date, subject: str) -> OptionInputIssue:
    return OptionInputIssue(
        kind, day, subject, "corrupt",
        f"{subject}: {lake_reason_words('not_in_the_lake')}", "not_in_the_lake", True,
    )


def _issue_took_too_long(kind: IssueKind, day: date, subject: str) -> OptionInputIssue:
    return OptionInputIssue(
        kind, day, subject, "building", f"{subject}: {lake_reason_words(TOOK_TOO_LONG)}", TOOK_TOO_LONG, True
    )


@dataclass(frozen=True)
class OptionInputCoverage:
    """What admission did for one member's option inputs: counts by kind, and every input it could not supply.

    ``reused`` were stored before this run; ``added`` were captured by it. ``days_from_archive`` of the contract days
    were read from a session container (the nightly archive) and not a per-contract file.
    """

    listings_expected: int = 0
    listings_reused: int = 0
    listings_added: int = 0
    days_expected: int = 0
    days_reused: int = 0
    days_added: int = 0
    days_from_archive: int = 0
    issues: tuple[OptionInputIssue, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "listings": {"expected": self.listings_expected, "reused": self.listings_reused, "added": self.listings_added},
            "contract_days": {
                "expected": self.days_expected,
                "reused": self.days_reused,
                "added": self.days_added,
                "from_archive": self.days_from_archive,
            },
        }


class OptionInputRefusal(StudyInputRefusal):
    """A member's option inputs were not supplied; nothing is computed from part of them.

    ``code`` is the most specific cause any issue shows (:data:`_CODE_ORDER`); ``retryable`` is whether asking again
    could change any of them. ``expected`` is how many inputs were asked for, for the sentence.
    """

    def __init__(self, issues: Sequence[OptionInputIssue], *, expected: int, coverage: OptionInputCoverage | None = None) -> None:
        if not issues:
            raise ValueError("a refusal names at least one issue")
        outcomes = {issue.outcome for issue in issues}
        code = next(refusal for outcome, refusal in _CODE_ORDER if outcome in outcomes)
        super().__init__(code, f"The lake cannot supply the option inputs: {summarise_issues(issues, expected, unit='option inputs')}.")
        self.issues = tuple(issues)
        self.option_coverage = coverage

    @property
    def retryable(self) -> bool:
        return any(issue.retryable for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "issues": [issue.as_dict() for issue in self.issues[:MAX_LISTED_ISSUES]],
            "issues_total": len(self.issues),
            "coverage": self.option_coverage.as_dict() if self.option_coverage is not None else None,
        }


# ---------------------------------------------------------------------------
# The price of a contract on a session
# ---------------------------------------------------------------------------


def regular_session_close(bars: Sequence[OptionBar], session: date) -> tuple[Decimal | None, Decimal, int | None]:
    """The price and regular-hours volume of one contract on one session: ``(close, volume, last_start_ms)``.

    Formula: with the scheduled session ``[open, close)`` and ``R`` the stored bars that lie wholly inside it
      (``open <= start`` and ``start + 60 000 <= close``): close = the close of the bar of ``R`` with the greatest start,
      volume = Σ volume over ``R``, ``last_start_ms`` = that bar's start. All three are ``None``/0 when ``R`` is empty. A
      bar starting at the session's close (the minute stamped 16:00) and the 16:00-16:15 tail of an ETF option are not in
      ``R``; an early close ends it at 13:00.
    Reference: owner decision on #3540 (2026-10-24): price each option at its last regular-hours trade. A bar is named by its
      close, its start plus a minute, and a stored bar that cannot be placed fails the whole day (ADR 0078 decisions 5 and
      17), so every stored bar must close on a whole minute of ``session``'s New York date or the day raises
      :class:`~app.options_backtest.lake_data.OptionBarsCannotBePlaced`.
    Canonical implementation: this file.
    Validated against: tests/research/options/test_option_inputs_fold.py (hand-worked sessions, an early close and a real
      captured SPY session); the price against the vendor's own daily VWAP is measured in
      tests/research/test_cross_sectional_options_lake_vs_provider.py.
    """
    unplaceable = [bar.start_ms for bar in bars if (bar.start_ms + _MS_PER_MINUTE) % _MS_PER_MINUTE or et_date_at_ms(bar.start_ms + _MS_PER_MINUTE) != session]
    if unplaceable:
        raise OptionBarsCannotBePlaced(
            f"{len(unplaceable)} of {len(bars)} stored bars cannot be placed in the session of {session.isoformat()}; "
            f"the first starts at {unplaceable[0]} ms"
        )
    window = session_window_for_date(session)
    regular = [bar for bar in bars if window.open_ms_utc <= bar.start_ms and bar.start_ms + _MS_PER_MINUTE <= window.close_ms_utc]
    if not regular:
        return None, Decimal(0), None
    last = max(regular, key=lambda bar: bar.start_ms)
    return last.close, sum((bar.volume for bar in regular), Decimal(0)), last.start_ms


@dataclass(frozen=True)
class OptionDayClose:
    """One contract's price on one session, folded from the stored day, with the digests of what it was folded from.

    ``contract`` is the vendor's spelling (``O:SPY...``). ``close`` is ``None`` when the contract made no regular-hours
    trade that session. ``sha256`` is the sha256 of the contract's original per-contract file, the same whether it was read
    from that file or from a session container; ``container_sha256`` is the container's when it was read from one.
    """

    contract: str
    session: date
    close: Decimal | None
    volume: Decimal
    sha256: str
    container_sha256: str | None = None

    def as_bar(self) -> dict[str, float] | None:
        """The bar the IV builder's price rule reads: ``close`` and ``volume`` as floats, or ``None`` with no price."""
        if self.close is None:
            return None
        return {"close": float(self.close), "volume": float(self.volume)}


def _fold_day(contract: str, session: date, stored: OptionDayBars) -> OptionDayClose:
    close, volume, _ = regular_session_close(stored.bars, session)
    container = stored.reference.container_sha256 if stored.reference is not None else None
    return OptionDayClose(contract, session, close, volume, stored.sha256, container)


# ---------------------------------------------------------------------------
# The chain, as the contract finder asks for it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListedOption:
    """One standard contract of a listing, as the contract finder reads it: ``ticker`` is the vendor's spelling."""

    ticker: str
    right: Literal["call", "put"]
    strike: float
    expiry: date


class ChainSource(Protocol):
    """What the contract finder needs of an option chain: the standard contracts alive on a session, in ticker order."""

    def listing_as_of(self, day: date) -> tuple[ListedOption, ...]: ...


def _project(listing: OptionListing) -> tuple[ListedOption, ...]:
    options = [
        ListedOption(f"O:{row.contract.occ_symbol}", row.contract.right, row.contract.strike_thousandths / 1000.0, row.contract.expiry_date)
        for row in listing.rows
        if row.standard and row.contract is not None
    ]
    return tuple(sorted(options, key=lambda option: option.ticker))


_STORED_FILE_ERRORS = (LakeAdmissionError, OptionListingFileError, OptionBarsFileError, CatalogUnavailableError, OSError, zipfile.BadZipFile)


class LakeChainSource:
    """The chain from the lake's stored listings: read, or captured now, for one underlying (:class:`ChainSource`).

    A listing is read through the catalog's admission. One the lake lacks is captured through ``capture`` (the lake's own
    writer, four at a time) and read back, unless its session is older than the options plan reaches: then the vendor is
    not asked and the input is refused (``outside_data_plan``). A stored listing that cannot be read is refused, not
    captured over: a captured session container embeds the listing it was made against, and a listing written afresh could
    disagree with it. Only a compact projection of each listing is kept (at most two), and the most recent whole listing for
    the contract-day capture that needs it. Two sets are kept apart: ``held`` is every listing the run ensured (to choose
    a contract, or as the proof a contract day may be captured against), and ``chosen_from`` the sha256 of the listings the
    contract picks were read from. Only the second is an input of the numbers, and so only it is in the digest: a rerun that
    finds every contract day stored needs no proof listing and must give the same digest.
    """

    _PROJECTIONS_KEPT = 2

    def __init__(
        self,
        underlying: str,
        *,
        now_ms: int,
        capture: LakeOptionCapture,
        check_cancelled: Callable[[], None] = lambda: None,
    ) -> None:
        self._underlying = underlying
        self._now_ms = now_ms
        self._capture = capture
        self._check_cancelled = check_cancelled
        self.held: dict[date, str] = {}
        self.chosen_from: dict[date, str] = {}
        self.listings_added: set[date] = set()
        self._projections: OrderedDict[date, tuple[ListedOption, ...]] = OrderedDict()
        self._latest: tuple[date, OptionListing] | None = None

    def listing_as_of(self, day: date) -> tuple[ListedOption, ...]:
        self.ensure([day])
        self.chosen_from[day] = self.held[day]
        return self._projection(day)

    def stored(self, day: date) -> OptionListing:
        """The whole stored listing of ``day``, which :meth:`ensure` has already made sure the lake holds."""
        if self._latest is not None and self._latest[0] == day:
            return self._latest[1]
        listing = self._read(day)
        if listing is None:
            raise OptionInputRefusal([_issue_not_there_after_capture("listing", day, self._underlying)], expected=1)
        self._remember(day, listing)
        return listing

    def ensure(self, days: Iterable[date]) -> None:
        """Make sure the lake holds a readable listing for each day: capture the absent ones, refuse if any cannot be had."""
        pending = sorted({day for day in days if day not in self.held})
        if not pending:
            return
        self._check_cancelled()
        issues: list[OptionInputIssue] = []
        absent: list[date] = []
        for day in pending:
            try:
                listing = self._read(day)
            except _STORED_FILE_ERRORS as error:
                issues.append(_issue_from_stored_file("listing", day, self._underlying, error))
                continue
            if listing is None:
                absent.append(day)
            else:
                self._remember(day, listing)
        floor = option_history_floor(et_date_at_ms(self._now_ms))
        outside = [day for day in absent if day < floor]
        issues.extend(
            OptionInputIssue(
                "listing", day, self._underlying, "unavailable_from_provider",
                f"{self._underlying}: {lake_reason_words('outside_data_plan')}", "outside_data_plan", False,
            )
            for day in outside
        )
        absent = [day for day in absent if day >= floor]
        if absent and not issues:
            issues.extend(self._capture_and_read(absent))
        if issues:
            raise OptionInputRefusal(issues, expected=len(pending))

    def _capture_and_read(self, days: Sequence[date]) -> list[OptionInputIssue]:
        issues: list[OptionInputIssue] = []
        try:
            results = self._capture.listings(days)
        except DownloadFailed as failed:
            return [_issue_took_too_long("listing", failed.session or days[0], self._underlying)]
        except CatalogUnavailableError as error:
            return [_issue_from_stored_file("listing", days[0], self._underlying, error)]
        for day in days:
            result: OptionCaptureResult | None = results.get(day)
            if result is None:
                continue  # not asked: an earlier capture failed and nothing after it was started
            if result.failure is not None:
                issues.append(_issue_from_failure("listing", day, self._underlying, result.failure))
                continue
            try:
                listing = self._read(day)
            except _STORED_FILE_ERRORS as error:
                issues.append(_issue_from_stored_file("listing", day, self._underlying, error))
                continue
            if listing is None:
                issues.append(_issue_not_there_after_capture("listing", day, self._underlying))
                continue
            if not result.reused:
                self.listings_added.add(day)
            self._remember(day, listing)
        return issues

    def _read(self, day: date) -> OptionListing | None:
        return read_option_listing(self._underlying, day)

    def _remember(self, day: date, listing: OptionListing) -> None:
        self.held[day] = listing.sha256
        self._projections[day] = _project(listing)
        self._projections.move_to_end(day)
        while len(self._projections) > self._PROJECTIONS_KEPT:
            self._projections.popitem(last=False)
        self._latest = (day, listing)

    def _projection(self, day: date) -> tuple[ListedOption, ...]:
        held = self._projections.get(day)
        if held is None:
            held = _project(self.stored(day))
            self._projections[day] = held
        return held

    def coverage_counts(self) -> tuple[int, int, int]:
        """``(expected, reused, added)`` listings."""
        return len(self.held), len(self.held) - len(self.listings_added), len(self.listings_added)


# ---------------------------------------------------------------------------
# One member's admitted option inputs
# ---------------------------------------------------------------------------


def option_input_identity() -> dict[str, Any]:
    """The policy the option inputs are read under, as it enters the study's identity and cache key and its data-source block."""
    return {
        "policy": OPTION_INPUT_POLICY,
        "listing_format": OPTION_LISTING_FILE_FORMAT,
        "bars_recipe": option_minute_trade_dch(),
        "price_rule": PRICE_RULE,
        "spot": _UNDERLYING_SOURCE,
        "contracts": _CONTRACTS_RULE,
    }


@dataclass(frozen=True, eq=False)
class MemberOptionInputs:
    """Everything one member's IV history is built from: the contracts picked, and each one's price on each session.

    ``bracket`` is the contract finder's frame (the expiry pair and the contracts picked for each session, off the raw
    close); ``days`` holds the folded price of each ``(contract, session)`` the frame names; ``listings_read`` the sha256
    of each listing the picks were chosen from (the proof listings a capture needed are not inputs of the numbers). Only
    folded prices are kept, never the bars.
    """

    ticker: str
    bracket: pd.DataFrame
    days: Mapping[tuple[str, date], OptionDayClose]
    listings_read: Mapping[date, str]
    coverage: OptionInputCoverage
    data_root_id: str

    def bar(self, contract: str, session: date) -> dict[str, float] | None:
        """The bar the price rule reads for ``contract`` on ``session``; ``None`` when it made no regular-hours trade.

        Raises ``KeyError`` for a contract-day this member never admitted: the IV build asks only for the ones the frame names.
        """
        return self.days[(contract, session)].as_bar()

    def digest(self) -> str:
        """sha256 of the root and of every stored file the inputs were read from (a container's member by its own sha256).

        A member's sha256 is the original per-contract file's whether the day was read from that file or from a session
        container, so compacting a session into a container does not move the digest.
        """
        return hash_payload(
            {
                "data_root_id": self.data_root_id,
                "listings": sorted([session_open_ms_utc(day), sha] for day, sha in self.listings_read.items()),
                "contract_days": sorted(
                    [contract.removeprefix("O:"), session_open_ms_utc(day), folded.sha256]
                    for (contract, day), folded in self.days.items()
                ),
            }
        )

    @property
    def source(self) -> Literal["archive", "kept", "mixed"]:
        """Where the contract days came from: the nightly archive's containers, files this study keeps, or both."""
        if self.coverage.days_from_archive == 0:
            return "kept"
        return "archive" if self.coverage.days_from_archive == len(self.days) else "mixed"

    def as_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "kind": "lake", "digest": self.digest(), "source": self.source, **self.coverage.as_dict()}


def needed_contract_days(bracket: pd.DataFrame) -> dict[date, tuple[str, ...]]:
    """The contracts the IV build prices on each session: the contracts of that session's row, in the frame's column order."""
    needed: dict[date, tuple[str, ...]] = {}
    for row in bracket.to_dict("records"):
        contracts = tuple(dict.fromkeys(row[column] for column in CONTRACT_COLUMNS if isinstance(row.get(column), str)))
        if contracts:
            needed[date.fromisoformat(row["date"])] = contracts
    return needed


def _no_check() -> None:
    return None


def _no_progress(_done: int, _total: int, _message: str) -> None:
    return None


def _data_root_id() -> str:
    marker = read_marker(option_read_root().parent.parent)
    return str(marker.data_root_id if marker is not None else active_root_id())


def admit_member_option_inputs(
    ticker: str,
    *,
    spot_rows: Sequence[Mapping[str, Any]],
    start_date: date,
    end_date: date,
    now_ms: int,
    capture: LakeOptionCapture,
    check_cancelled: Callable[[], None] = _no_check,
    on_progress: Callable[[int, int, str], None] = _no_progress,
) -> MemberOptionInputs:
    """Choose a member's contracts from the lake's listings and fold each one's price on each session, capturing what the lake lacks.

    ``spot_rows`` are the member's raw (unadjusted) daily rows: a strike rule compares against the price the contract was
    listed against (ADR 0078 decision 6). Order: the contracts are picked session by session (:func:`find_bracket_contracts`
    over :class:`LakeChainSource`), every contract-day already stored is read and folded, the listings the missing ones need
    are captured four at a time, and then each session's missing contract days are captured, in date order, up to eight at
    once. Blocking: call from a worker thread. ``check_cancelled`` raises to abandon the study; a stop that reaches a capture
    surfaces through it.

    Raises :class:`OptionInputRefusal` with every input that could not be supplied when it knows them all without asking
    the vendor (a stored file that does not read, an input older than the plan), else with the first session whose capture
    failed.
    """
    source = LakeChainSource(ticker, now_ms=now_ms, capture=capture, check_cancelled=check_cancelled)
    try:
        bracket = find_bracket_contracts(ticker, start_date.isoformat(), end_date.isoformat(), source, list(spot_rows))
    except DownloadStopped:
        check_cancelled()
        raise
    needed = needed_contract_days(bracket) if not bracket.empty else {}
    sessions = sorted(needed)
    expected = sum(len(contracts) for contracts in needed.values())
    folded: dict[tuple[str, date], OptionDayClose] = {}
    reused = 0
    issues: list[OptionInputIssue] = []

    def read(contract: str, day: date) -> OptionDayClose | None:
        stored = read_option_day_bars(OptionContract.from_occ_symbol(contract.removeprefix("O:")), day, listed_under=ticker)
        return None if stored is None else _fold_day(contract, day, stored)

    missing: dict[date, list[str]] = {}
    for day in sessions:
        check_cancelled()
        for contract in needed[day]:
            try:
                found = read(contract, day)
            except _STORED_FILE_ERRORS as error:
                issues.append(_issue_from_stored_file("contract_day", day, contract, error))
                continue
            if found is None:
                missing.setdefault(day, []).append(contract)
            else:
                folded[(contract, day)] = found
                reused += 1
    floor = option_history_floor(et_date_at_ms(now_ms))
    if issues or any(day < floor for day in missing):
        issues.extend(
            OptionInputIssue(
                "contract_day", day, contract, "unavailable_from_provider",
                f"{contract}: {lake_reason_words('outside_data_plan')}", "outside_data_plan", False,
            )
            for day in missing
            if day < floor
            for contract in missing[day]
        )
        raise OptionInputRefusal(issues, expected=expected + len(source.held))

    try:
        source.ensure(list(missing))
    except DownloadStopped:
        check_cancelled()
        raise
    added = 0
    for position, day in enumerate(sessions, start=1):
        if day in missing:
            check_cancelled()
            issues = []
            try:
                results = capture.contract_days(
                    day, [OptionContract.from_occ_symbol(contract.removeprefix("O:")) for contract in missing[day]], source.stored(day)
                )
            except DownloadStopped:
                check_cancelled()
                raise
            except DownloadFailed:
                results = []
                issues.extend(_issue_took_too_long("contract_day", day, contract) for contract in missing[day])
            except CatalogUnavailableError as error:
                results = []
                issues.append(_issue_from_stored_file("contract_day", day, missing[day][0], error))
            for contract_object, result in results:
                contract = f"O:{contract_object.occ_symbol}"
                if result.failure is not None:
                    issues.append(_issue_from_failure("contract_day", day, contract, result.failure))
                    continue
                try:
                    found = read(contract, day)
                except _STORED_FILE_ERRORS as error:
                    issues.append(_issue_from_stored_file("contract_day", day, contract, error))
                    continue
                if found is None:
                    issues.append(_issue_not_there_after_capture("contract_day", day, contract))
                    continue
                folded[(contract, day)] = found
                if result.reused:
                    reused += 1
                else:
                    added += 1
            if issues:
                raise OptionInputRefusal(issues, expected=expected + len(source.held))
        on_progress(position, len(sessions), f"{ticker}: {position} of {len(sessions)} option sessions")

    listings_expected, listings_reused, listings_added = source.coverage_counts()
    coverage = OptionInputCoverage(
        listings_expected=listings_expected,
        listings_reused=listings_reused,
        listings_added=listings_added,
        days_expected=expected,
        days_reused=reused,
        days_added=added,
        days_from_archive=sum(1 for day in folded.values() if day.container_sha256 is not None),
    )
    return MemberOptionInputs(
        ticker=ticker,
        bracket=bracket,
        days=folded,
        listings_read=dict(source.chosen_from),
        coverage=coverage,
        data_root_id=_data_root_id(),
    )


# ---------------------------------------------------------------------------
# The risk-free rate: live, not in the lake
# ---------------------------------------------------------------------------


@dataclass
class RateTally:
    """Which source priced each trading day's risk-free rate in one member's IV history, and the values used.

    FRED is asked live and keeps no archive, so the rate is the one option input the lake cannot hold. It is counted here,
    apart from the lake inputs, with a digest of the (day, rate) pairs used: a later run that was priced at other rates
    shows another digest. A default rate taken while a FRED key is configured is a FRED outage (``days_fetch_failed``) and
    degrades the member; a default with no key configured is the same on every attempt and does not.
    """

    _days: dict[str, tuple[float, str, bool]] = field(default_factory=dict)

    def record(self, day: str, resolution: RateResolution) -> None:
        self._days[day] = (resolution.rate, resolution.source, resolution.fetch_failed)

    @property
    def days_from_fred(self) -> int:
        return sum(1 for _, source, _ in self._days.values() if source == RATE_SOURCE_FRED)

    @property
    def days_from_default(self) -> int:
        return sum(1 for _, source, _ in self._days.values() if source == RATE_SOURCE_DEFAULT)

    @property
    def days_fetch_failed(self) -> int:
        """Days whose rate is the default only because FRED did not answer; asking again could give FRED's rate."""
        return sum(1 for _, _, failed in self._days.values() if failed)

    @property
    def degraded(self) -> bool:
        return self.days_fetch_failed > 0

    def values_digest(self) -> str | None:
        return hash_payload(sorted([day, rate] for day, (rate, _, _) in self._days.items())) if self._days else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "provider",
            "provider": "fred",
            "days_from_fred": self.days_from_fred,
            "days_from_default": self.days_from_default,
            "days_fetch_failed": self.days_fetch_failed,
            "values_digest": self.values_digest(),
        }


def combined_rate_block(tallies: Mapping[str, RateTally]) -> dict[str, Any]:
    """The run's risk-free rate source: each member's day counts summed, and a digest of the members' value digests."""
    ordered = sorted(tallies.items())
    seen = [(ticker, digest) for ticker, tally in ordered if (digest := tally.values_digest()) is not None]
    return {
        "kind": "provider",
        "provider": "fred",
        "days_from_fred": sum(tally.days_from_fred for _, tally in ordered),
        "days_from_default": sum(tally.days_from_default for _, tally in ordered),
        "days_fetch_failed": sum(tally.days_fetch_failed for _, tally in ordered),
        "values_digest": hash_payload(seen) if seen else None,
    }


# ---------------------------------------------------------------------------
# The data-source block
# ---------------------------------------------------------------------------


def option_inputs_block(
    inputs: Mapping[str, MemberOptionInputs], refused: Mapping[str, OptionInputRefusal], order: Sequence[str]
) -> dict[str, Any]:
    """The run's option inputs as the page reads them: the lake, the policy and recipes, counts, and each member's own receipt.

    ``inputs`` are the admitted members, ``refused`` the members whose option inputs could not be supplied (each with its
    first issues and the total); both are listed in ``order``, the request's. The digest is the digest of the admitted
    members' digests.
    """
    members: list[dict[str, Any]] = []
    for ticker in (ticker for ticker in order if ticker in inputs or ticker in refused):
        if ticker in inputs:
            members.append({"status": "admitted", **inputs[ticker].as_dict()})
        else:
            refusal = refused[ticker]
            members.append(
                {
                    "ticker": ticker,
                    "status": "excluded",
                    "code": refusal.code,
                    "reason": refusal.reason,
                    "retryable": refusal.retryable,
                    "issues": [issue.as_dict() for issue in refusal.issues[:MAX_LISTED_ISSUES]],
                    "issues_total": len(refusal.issues),
                }
            )
    admitted = [inputs[ticker] for ticker in order if ticker in inputs]
    total = lambda key, name: sum(getattr(member.coverage, f"{key}_{name}") for member in admitted)  # noqa: E731
    return {
        "kind": "lake",
        **option_input_identity(),
        "market": "usa_options",
        "price_adjustment_mode": OPTION_PRICE_ADJUSTMENT_MODE,
        "price_rule_words": PRICE_RULE_WORDS,
        "digest": hash_payload([(member.ticker, member.digest()) for member in admitted]) if admitted else None,
        "listings": {"expected": total("listings", "expected"), "reused": total("listings", "reused"), "added": total("listings", "added")},
        "contract_days": {
            "expected": total("days", "expected"),
            "reused": total("days", "reused"),
            "added": total("days", "added"),
            "from_archive": total("days", "from_archive"),
        },
        "members": members,
    }
