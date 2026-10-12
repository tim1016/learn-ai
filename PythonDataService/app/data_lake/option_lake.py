"""What option bars and option listings share in the data lake (#3030).

The lake holds two things about options beyond its stock tree: one contract's
minute bars for one session (``option_bars``) and the contracts listed for an
underlying as of a date (``option_listing``). Both use the lake's existing
claim, lease, fencing, publication and admission machinery as it stands, with
no database migration, under a market name of their own. Every reader that
lists or counts artifacts filters on ``Market``, so a distinct name keeps these
rows out of every stock answer.

This module holds the rules the two kinds have in common: the market name, the
failure type, the "is the session over" rule, the plan's rolling window, the
steps from a claim to a lease this caller holds, and the publication step. The
stock path does the same steps in ``ensure_data._process_minute_trade_artifact``;
the functions it calls are called here as they are.

Why a capture does not produce an artifact (``OptionDataFailure.reason``), each
once, here. These are refused before any claim, so no row is made and the vendor is
not asked:

* ``symbol_too_long``: the symbol has more characters than the catalog's Symbol column holds.
* ``not_a_session``: the exchange was closed that day.
* ``session_not_over``: the session's New York calendar date has not passed yet
  (:func:`option_capture_opens_ms`).
* ``outside_data_plan``: the plan's rolling window does not reach the session; a bars
  session before ``option_history_floor`` is refused before any claim, and a day at the
  edge the vendor's own 403 names (bars or listing) is recorded on the row, asked again
  on every call and never latched into ``fetch_timeout``.
* ``listing_not_captured`` (bars): the lake holds no usable listing of that underlying for
  that date, so the contract cannot be vouched for; capture the listing first.
* ``not_listed`` (bars): that listing does not hold the contract, and the vendor says the
  same empty thing for a contract that never existed as for one that did not trade.

These are recorded on the claimed row, which is failed and never left fetching:

* ``provider_auth_error``: the vendor refused the key (401), or the key cannot be sent at all
  (empty, or holding a space or a character that is not printable ASCII; no request is made); a
  run should stop.
* ``provider_entitlement_error``: a 403 that is not the plan-timeframe answer.
* ``provider_rate_limited``: the vendor said to slow down (429).
* ``unknown_symbol``: the vendor does not know the ticker (404, or status NOT_FOUND).
* ``provider_api_error``: any other trouble with the vendor (a 5xx, a timeout, a body that
  is not a confirmed well-formed answer, a listing whose paging ran past
  :data:`LISTING_FETCH_BUDGET_MS`); asked again, up to ``MAX_CLAIM_RETRIES`` times.
* ``provider_no_data`` (listing): the vendor listed no contracts; that is not pinned as a fact.
* ``validation_failed``: what the vendor sent cannot be stored as a fact (corrupt bars, a
  price the file cannot hold or would store as 0, a listing row with no ticker or a ticker
  listed twice).
* ``io_error``: the vendor answered and the file could not be written (a full disk, a lake or
  staging folder that cannot be made, staging on another filesystem than the lake). The text
  names the kind of error and the system's reason and no path; the log has the rest. Asked
  again like any failure, up to ``MAX_CLAIM_RETRIES`` times.

These come from the claim protocol, when nothing was fetched:

* ``lease_timeout``: another worker holds the row, or this writer lost its lease while
  working and published or recorded nothing: not a publish, and not a failure it met (a
  failure it met is the row's only when it still holds the lease to record it).
* ``fetch_timeout``: the row has spent its attempts; the vendor is not asked again.
"""

from __future__ import annotations

import asyncio
import io
import logging
import lzma
import time
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.atomic import ArtifactLeaseLostError, AtomicRenameUnsafeError, publish_artifact
from app.data_lake.ensure_data import _LEASE_TTL_MS, _MAX_CLAIM_RETRIES, _WORKER_ID
from app.data_lake.path_policy import lake_root_for, resolve_lake_root, resolve_staging_root
from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
)
from app.data_lake.polygon_option_contracts import PolygonContractRowError
from app.data_lake.polygon_option_http import PolygonOutsidePlanError
from app.data_lake.types import (
    MAX_SYMBOL_LENGTH,
    ArtifactFailureReason,
    ArtifactRecord,
    PriceAdjustmentMode,
)
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms

logger = logging.getLogger(__name__)

#: The catalog's market name for option rows. Stock rows are ``usa``.
OPTION_MARKET = "usa_options"
OPTION_PROVIDER = "polygon"
#: Option prices are the trades as they printed; there is nothing to adjust, and
#: the row's mode must equal the lake root's directory name for admission.
OPTION_PRICE_ADJUSTMENT_MODE: PriceAdjustmentMode = "raw"

#: How many years back the owner's options plan serves minute bars. It comes from a
#: probe of the owner's plan on 2026-10-12: a session of 2024-10-11 answered 403
#: NOT_AUTHORIZED ("this data timeframe") and 2024-10-14 answered with bars. The
#: window rolls forward every day, so what is fetched must be kept. When the plan
#: changes, probe a session near the new boundary and change this one number; the
#: floor below and every refusal that rests on it follow.
OPTION_HISTORY_YEARS = 2
#: Days of headroom over the plan's boundary, as ``polygon_fetcher`` keeps for stocks.
_HISTORY_FLOOR_MARGIN_DAYS = 2

#: The most characters of a failure's text kept on a row. The vendor's body is already cut to 200
#: by the shared fetcher; this bounds the rest (our own messages) so none can grow without limit.
MAX_FAILURE_TEXT_CHARS = 1000

#: One staging request id for the whole process, as the stock path shares one per run:
#: staging leaves its folders behind (nothing sweeps them), so a fresh id per publish
#: would leave a new set of folders for every file. A publish stages under its lease
#: generation (``attempt_<generation>``), so two writers of one file in this process,
#: the winner and a writer that lost its lease, never stage to the same path.
_PROCESS_REQUEST_ID = uuid4()

WORKER_ID = _WORKER_ID
LEASE_TTL_MS = _LEASE_TTL_MS
MAX_CLAIM_RETRIES = _MAX_CLAIM_RETRIES

#: What a listing's paging leaves of the lease for everything after its last request. The pages are asked
#: one after another while the lease stays fixed, and a listing that finishes after its lease is thrown away
#: at publication. After the last request starts there is still that request (up to its 30 s timeout), building the
#: file, staging it with an fsync, and the publish itself, so the paging stops this long before the lease ends.
LISTING_PUBLISH_MARGIN_MS = 60_000
#: The time a whole listing fetch may take: the lease less the margin above. Past it the fetch ends as a
#: failed row (``provider_api_error``) before another page is asked, and a later ask tries again.
LISTING_FETCH_BUDGET_MS = LEASE_TTL_MS - LISTING_PUBLISH_MARGIN_MS

#: ``LastError`` values that are retried however many times they were spent. A day
#: the vendor says is outside the plan is environmental, not data: the owner upgrading
#: the plan clears it, so it must not latch into a terminal ``fetch_timeout`` after
#: three asks. Only a day at the edge of the floor reaches the vendor to be told so
#: (an older one is refused before any claim). Known gap: the exemption lasts only while
#: the row's last error is this one, and every ask counts as an attempt (the shared
#: reclaim has no reset for it). So a row told "outside the plan" on two asks and then hit
#: by one transient failure (a 429, a 5xx, a malformed answer) has spent its three
#: attempts, and its next ask is ``fetch_timeout`` with no vendor call. One plan answer
#: before the failure is survived; two are not.
NOT_LATCHED_ERRORS = frozenset({"outside_data_plan"})

#: The lake's own reasons (``ArtifactFailureReason``) and the six an option capture adds; the module docstring says what each means.
OptionFailureReason = (
    ArtifactFailureReason
    | Literal[
        "outside_data_plan",
        "session_not_over",
        "not_a_session",
        "symbol_too_long",
        "listing_not_captured",
        "not_listed",
    ]
)


@dataclass(frozen=True)
class OptionDataFailure:
    """Why a capture did not produce an artifact; the module docstring says what each ``reason`` means.

    ``symbol`` is the contract's symbol, or the underlying for a listing, and
    ``trading_date`` the session or the listing's as-of date. The lake's own
    ``ArtifactFailure`` is not used because its ``reason`` is a closed list the
    served OpenAPI contract carries, and this adds reasons the stock path has no use for.
    """

    reason: OptionFailureReason
    detail: str
    symbol: str
    trading_date: date
    attempt_count: int = 0


@dataclass(frozen=True)
class OptionCaptureResult:
    """A capture's answer: the artifact (fetched now or reused), or the failure."""

    record: ArtifactRecord | None
    failure: OptionDataFailure | None
    reused: bool = False

    def __post_init__(self) -> None:
        if (self.record is None) == (self.failure is None):
            raise ValueError("a capture result holds exactly one of an artifact record and a failure")

    @classmethod
    def failed(cls, failure: OptionDataFailure) -> OptionCaptureResult:
        return cls(record=None, failure=failure)


def current_ms() -> int:
    return int(time.time() * 1000)


def option_history_floor(today: date) -> date:
    """The oldest session the options plan is composed against, as of ``today``.

    Two years back, with the two days of headroom ``polygon_history_floor`` keeps
    over the stock plan's five: the boundary day itself is excluded, and a window
    composed just before an ET midnight must still be inside it. A session older
    than this is refused before any claim (:func:`plan_window_refusal`); a day at
    the edge is left to the vendor, whose 403 says whether the plan reaches it.
    """
    try:
        anniversary = today.replace(year=today.year - OPTION_HISTORY_YEARS)
    except ValueError:
        # 29 February with a non-leap anniversary year.
        anniversary = today.replace(year=today.year - OPTION_HISTORY_YEARS, day=28)
    return anniversary + timedelta(days=_HISTORY_FLOOR_MARGIN_DAYS)


def symbol_refusal(symbol: str, trading_date: date) -> OptionDataFailure | None:
    """A symbol the catalog's ``Symbol`` column cannot hold is refused before any claim."""
    if len(symbol) > MAX_SYMBOL_LENGTH:
        return OptionDataFailure(
            "symbol_too_long",
            f"{symbol!r} is {len(symbol)} characters; the catalog stores at most {MAX_SYMBOL_LENGTH}",
            symbol,
            trading_date,
        )
    return None


def option_capture_opens_ms(trading_date: date) -> int:
    """The first instant a session's option data may be captured: New York midnight after the session's date.

    The vendor's data runs behind and its late prints are unmeasured, and an empty answer stored
    too early is a permanent wrong "never traded" (#3030); so a session is taken only once its New
    York calendar date has passed, whatever time it closed (an early close follows the same rule).
    The canonical calendar gives the session's close, the New York zone gives the date it falls on
    and the midnight that ends it, so a day the clocks change is never a fixed offset out.
    Raises ``LookupError`` for a day that is not a session.
    """
    return et_day_end_ms(et_date_at_ms(session_close_ms_utc(trading_date)))


def session_refusal(symbol: str, trading_date: date, *, now_ms: int) -> OptionDataFailure | None:
    """A session whose New York date has not passed is never fetched or admitted, and no claim is made for it."""
    try:
        opens_ms = option_capture_opens_ms(trading_date)
    except LookupError:
        return OptionDataFailure(
            "not_a_session", f"{trading_date.isoformat()} is not a NYSE session", symbol, trading_date
        )
    if now_ms < opens_ms:
        return OptionDataFailure(
            "session_not_over",
            f"{trading_date.isoformat()} is not over in New York yet; its option data is taken from "
            f"New York midnight after it, {opens_ms}, and it is {now_ms}",
            symbol,
            trading_date,
        )
    return None


def refusal_before_claim(symbol: str, trading_date: date, *, now_ms: int) -> OptionDataFailure | None:
    """The refusals that cost no claim: an unstorable symbol, a day with no session, a session not over."""
    return symbol_refusal(symbol, trading_date) or session_refusal(symbol, trading_date, now_ms=now_ms)


def plan_window_refusal(symbol: str, trading_date: date, *, now_ms: int) -> OptionDataFailure | None:
    """A session older than the plan reaches is refused before any claim, with no vendor call.

    Minute bars are windowed; the contract listing is reference data that reaches back
    before the price floor, so only the bars path asks this. Asking the vendor for such a
    day would only be told no, and a row for it would pile up attempts for nothing.
    """
    floor = option_history_floor(et_date_at_ms(now_ms))
    if trading_date < floor:
        return OptionDataFailure(
            "outside_data_plan",
            f"{trading_date.isoformat()} is before {floor.isoformat()}, the oldest session the options plan serves",
            symbol,
            trading_date,
        )
    return None


def vendor_reason(error: PolygonFetchError) -> OptionFailureReason:
    """The typed reason for a vendor error.

    A 403 is ``outside_data_plan`` only when the vendor's own body says the plan does
    not include this data timeframe; any other 403 is a real entitlement problem. The
    date alone never decides it.
    """
    if isinstance(error, PolygonAuthError):
        return "provider_auth_error"
    if isinstance(error, PolygonOutsidePlanError):
        return "outside_data_plan"
    if isinstance(error, PolygonEntitlementError):
        return "provider_entitlement_error"
    if isinstance(error, PolygonContractRowError):
        return "validation_failed"
    if isinstance(error, PolygonRateLimitedError):
        return "provider_rate_limited"
    if isinstance(error, PolygonUnknownSymbolError):
        return "unknown_symbol"
    return "provider_api_error"


def storable_failure_text(text: str) -> str:
    """A failure's text as a row can hold it: no NUL, nothing UTF-8 cannot encode, and a bounded length.

    Postgres refuses a NUL in text, and a character no UTF-8 can encode (a lone surrogate from a
    JSON escape) fails the driver, so a vendor's text carrying either would make recording the
    failure itself raise and leave the row fetching. At most :data:`MAX_FAILURE_TEXT_CHARS` are kept.
    """
    storable = text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
    return storable[:MAX_FAILURE_TEXT_CHARS]


async def record_failure(
    *,
    artifact_id: int,
    lease_generation: int,
    reason: OptionFailureReason,
    detail: str,
    symbol: str,
    trading_date: date,
) -> OptionDataFailure:
    """Fail the claimed row under its generation (fenced), and say what became of the failure.

    The text is made storable first (:func:`storable_failure_text`), and the answer carries the text stored.
    The catalog refuses the update when this writer no longer holds the generation (its lease was taken
    over while it fetched or validated). Then the failure was not recorded and is not the row's: the row
    is the new owner's, and the answer is ``lease_timeout``, as a publish by a writer that lost its lease
    answers, never the failure this writer met.
    """
    detail = storable_failure_text(detail)
    recorded = await catalog_client.fail_artifact(
        artifact_id, reason, detail, worker_id=WORKER_ID, lease_generation=lease_generation
    )
    if not recorded:
        return OptionDataFailure(
            "lease_timeout",
            f"the lease on this row was taken over before this writer's failure ({reason}) could be recorded; "
            "the row is the new owner's",
            symbol,
            trading_date,
            attempt_count=1,
        )
    return OptionDataFailure(reason, detail, symbol, trading_date, attempt_count=1)


async def fail_row(
    *,
    artifact_id: int,
    lease_generation: int,
    reason: OptionFailureReason,
    detail: str,
    symbol: str,
    trading_date: date,
) -> OptionCaptureResult:
    """:func:`record_failure`, answered as a capture result."""
    return OptionCaptureResult.failed(
        await record_failure(
            artifact_id=artifact_id,
            lease_generation=lease_generation,
            reason=reason,
            detail=detail,
            symbol=symbol,
            trading_date=trading_date,
        )
    )


@dataclass(frozen=True)
class HeldLease:
    """A row this caller owns at ``lease_generation``: publish, or fail, with that generation."""

    artifact_id: int
    lease_generation: int


async def claim_or_take_over(
    *,
    claim: Callable[[], Awaitable[int | None]],
    select_complete: Callable[[], Awaitable[ArtifactRecord | None]],
    read_claim_state: Callable[[], Awaitable[catalog_client.ArtifactClaimState | None]],
    stored_file_reads: Callable[[ArtifactRecord], Awaitable[bool]],
    data_contract_hash: str,
    symbol: str,
    trading_date: date,
) -> HeldLease | OptionCaptureResult:
    """Claim the row, or reuse, refresh or reclaim the one that is there; the stock path's order exactly.

    1. ``claim`` wins the row at the initial generation.
    2. A lost claim onto a complete row reuses it when its recipe is this recipe
       and its bytes are still admitted and readable; otherwise
       ``refresh_complete_artifact`` takes it over at a new generation.
    3. A lost claim onto a row that is not complete (failed, or its lease ran out)
       goes through ``reclaim_after_lost_claim``, the one reclaim protocol every
       artifact kind uses.

    Returns the lease to publish under, or a finished result (a reuse, or why
    the row could not be had).
    """
    artifact_id = await claim()
    if artifact_id is not None:
        return HeldLease(artifact_id, catalog_client.INITIAL_LEASE_GENERATION)

    cached = await select_complete()
    if cached is not None:
        if cached.data_contract_hash == data_contract_hash and await stored_file_reads(cached):
            return OptionCaptureResult(record=cached, failure=None, reused=True)
        prior = await catalog_client.refresh_complete_artifact(
            artifact_id=cached.id,
            worker_id=WORKER_ID,
            lease_ttl_ms=LEASE_TTL_MS,
            expected_data_contract_hash=cached.data_contract_hash,
        )
        if prior is None:
            return OptionCaptureResult.failed(
                OptionDataFailure("lease_timeout", "the day changed during rebuild; retry", symbol, trading_date)
            )
        return HeldLease(cached.id, prior.new_lease_generation)

    reclaim = await catalog_client.reclaim_after_lost_claim(
        read_claim_state,
        worker_id=WORKER_ID,
        lease_ttl_ms=LEASE_TTL_MS,
        max_retries=MAX_CLAIM_RETRIES,
        retry_ceiling_exempt_errors=NOT_LATCHED_ERRORS,
        data_contract_hash=data_contract_hash,
    )
    if isinstance(reclaim, catalog_client.ReclaimRefused):
        return OptionCaptureResult.failed(
            OptionDataFailure(reclaim.reason, reclaim.detail, symbol, trading_date, attempt_count=reclaim.attempt_count)
        )
    return HeldLease(reclaim.artifact_id, reclaim.lease_generation)


def zip_member_bytes(payload: bytes, member: str, error: type[ValueError]) -> bytes:
    """The bytes of the one member a lake option file holds; any other file raises ``error``, the cause chained.

    A file that is not a zip, a damaged or truncated one, one with an unsupported or encrypted
    member, and one whose members are not exactly ``[member]`` are the same answer: it is not the
    file the lake writes. The zip module raises a different error for each kind of damage, and
    none of them escapes.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            raw = zf.read(member) if names == [member] else None
    except (
        zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, NotImplementedError, RuntimeError, OSError, ValueError,
        ArithmeticError,
    ) as exc:
        raise error(f"{member}: not a readable file ({type(exc).__name__}: {exc})") from exc
    if raw is None:
        raise error(f"expected one member {member!r}, found {names!r}")
    return raw


def option_read_root(lake_root: Path | None = None) -> Path:
    """The raw lake root a reader reads option files from: the configured one, or the one it is handed.

    A root it is handed must be a lake's raw root (``<base>/lake/raw``, the shape the catalog's
    admission recognises, on this machine or an alternate mount of it). Any other path would be
    read by the default admission without being admitted, so it is a ``ValueError``.
    """
    if lake_root is None:
        return resolve_lake_root(OPTION_PRICE_ADJUSTMENT_MODE)
    root = lake_root.absolute()
    if root.name != OPTION_PRICE_ADJUSTMENT_MODE or lake_root_for(root / "any") != root:
        raise ValueError(f"lake_root must be a lake's raw root (<base>/lake/{OPTION_PRICE_ADJUSTMENT_MODE}), got {lake_root}")
    return root


def writable_raw_roots() -> tuple[Path, Path]:
    """The raw lake root and its staging root, created if this is the first write.

    The twin of ``ensure_data._writable_lake_roots``, which takes a ``DataRunSpec``
    for the one thing it reads from it (the mode); option data is always raw.
    """
    lake_root = resolve_lake_root(OPTION_PRICE_ADJUSTMENT_MODE)
    staging_root = resolve_staging_root(OPTION_PRICE_ADJUSTMENT_MODE)
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    return lake_root, staging_root


def _write_failure_words(error: OSError | AtomicRenameUnsafeError) -> str:
    """What failed, for the row and the wire: the kind of error and the system's reason, and no path.

    An ``OSError``'s text carries the path of the file it met (``strerror`` is the reason alone); the
    full text goes to the log.
    """
    if isinstance(error, OSError) and error.strerror:
        return f"{type(error).__name__}: {error.strerror}"
    return f"{type(error).__name__}: {error}"


async def publish_option_artifact(
    *,
    payload: bytes,
    rel_path: PurePosixPath,
    lease: HeldLease,
    row_count: int,
    first_bar_start_ms: int,
    last_bar_start_ms: int,
    data_contract_hash: str,
    symbol: str,
    trading_date: date,
) -> str | OptionDataFailure:
    """Publish one claimed artifact under its lease: bytes onto the lake and the receipt into the catalog.

    The twin of ``ensure_data._publish_under_lease`` (which takes a ``DataRunSpec``
    only for a request id and the mode): the same single call into
    ``atomic.publish_artifact``, where staging, the row lock, the lease and
    generation check, the rename and the completion receipt happen as one
    operation. Returns the file's sha256. A writer that lost its lease gets a ``lease_timeout``
    failure instead and must not fail the row; it is the winner's now.

    A file that cannot be written (the lake or staging folder cannot be made, the disk is full, the
    staging root is on another filesystem than the lake) is this writer's own failure on a row it
    holds, so the row is failed under its generation as ``io_error`` and that failure is returned,
    as every vendor failure is, and the row is never left fetching until its lease runs out. An
    outage of the catalog is not a failed write and propagates.
    """
    try:
        lake_root, staging_root = writable_raw_roots()
        file_sha = await publish_artifact(
            content=payload,
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel_path,
            request_id=_PROCESS_REQUEST_ID,
            worker_id=WORKER_ID,
            attempt=lease.lease_generation,
            artifact_id=lease.artifact_id,
            lease_generation=lease.lease_generation,
            row_count=row_count,
            first_bar_start_ms=first_bar_start_ms,
            last_bar_start_ms=last_bar_start_ms,
            data_contract_hash=data_contract_hash,
        )
    except ArtifactLeaseLostError as e:
        return OptionDataFailure("lease_timeout", str(e), symbol, trading_date, attempt_count=1)
    except (OSError, AtomicRenameUnsafeError) as e:
        logger.warning(
            "data_lake.option_lake: the file could not be written; failing the row",
            extra={
                "action": "option_file_write_failed",
                "file_path": str(rel_path),
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        return await record_failure(
            artifact_id=lease.artifact_id,
            lease_generation=lease.lease_generation,
            reason="io_error",
            detail=f"the file for {symbol} on {trading_date.isoformat()} could not be written "
            f"({_write_failure_words(e)})",
            symbol=symbol,
            trading_date=trading_date,
        )
    return file_sha


async def stored_file_still_reads(
    record: ArtifactRecord, rel_path: PurePosixPath, parse: Callable[[bytes], object]
) -> bool:
    """Is a complete row's file still admitted by the catalog and readable?

    The gate before reusing a row. A file that is gone, changed, orphaned, or does
    not parse is not a hit: the refresh path rebuilds it. An outage of the catalog
    is not a verdict on the file and propagates. That the bytes are the row's own
    is the admission's job: it admits only bytes that have a committed receipt, and
    the row asked about is the only one for its recipe.
    """
    if record.file_path != str(rel_path):
        return False
    path = resolve_lake_root(OPTION_PRICE_ADJUSTMENT_MODE) / Path(*rel_path.parts)

    def check() -> None:
        parse(read_committed_bytes(path))

    try:
        await asyncio.to_thread(check)
    except (LakeAdmissionError, OSError, zipfile.BadZipFile, ValueError):
        logger.warning(
            "data_lake.option_lake: a stored option file is not admitted or does not read; rebuilding it",
            extra={"action": "rebuild_unreadable_option_file", "file_path": record.file_path},
        )
        return False
    return True
