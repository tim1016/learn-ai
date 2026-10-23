"""Resumable full-listing session capture with brief publication leases (#3427).

A same-host advisory lock fences the collector for its whole lifetime; process
death releases it. Validated ticker outcomes are fsynced independently under
the listing/recipe identity. There is no catalog lease during provider work,
no staging cleanup by a losing collector, and no staged coverage is readable.
Like ``chain_download`` it captures a session's listing, contracts and underlying;
a whole session is complete only with all three (:func:`read_session_completeness`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.bar_validation import CorruptVendorBarsError
from app.data_lake.option_bars import encode_option_day, parse_option_day_zip
from app.data_lake.option_lake import (
    MAX_CLAIM_RETRIES,
    MAX_VENDOR_REQUESTS_IN_FLIGHT,
    TERMINAL_PROVIDER_REASONS,
    OptionCaptureResult,
    OptionDataFailure,
    OptionFailureReason,
    current_ms,
    option_history_floor,
    refusal_before_claim,
    terminal_provider_reason,
    vendor_reason,
    vendor_request_gate,
)
from app.data_lake.option_listing import OptionListing, ensure_option_listing, read_option_listing
from app.data_lake.option_session import OptionSession, check_member_size, option_session_dch, read_option_session
from app.data_lake.option_session_repack import admitted_legacy_members, complete_session_record, publish_option_session
from app.data_lake.option_session_staging import (
    DEFAULT_STAGING_MAX_BYTES,
    TICKER_RESERVATION_BYTES,
    OptionSessionStaging,
    StagingCapacityError,
    StagingReservation,
)
from app.data_lake.path_policy import resolve_staging_root
from app.data_lake.polygon_fetcher import PolygonFetchError
from app.data_lake.polygon_option_bars import fetch_listed_option_minute_bars, require_listed_option_symbol
from app.data_lake.polygon_option_http import PolygonTimeoutError
from app.engine.live.identity import confine_path_to_root
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options_backtest.chain_download import Outcome, capture_underlying
from app.options_backtest.lake_data import LakeRunData
from app.utils.background_loop import background_loop
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

#: Tries for one contract's request when the vendor does not answer in time. They share one transient
#: attempt, so a vendor outage still fails the session and leaves the ticker's budget for a later pass.
TIMEOUT_TRIES_PER_REQUEST = 3


def read_session_completeness(
    underlying: str, trading_date: date, *, lake_root: Path | None = None
) -> tuple[OptionSession | None, bool]:
    """The admitted container, and whether the whole session is captured: every listed contract and the underlying day.

    The underlying day is read through the run reader's admission, so a corrupt or unadmitted day raises instead
    of reading as complete. Synchronous; call it off the event loop.
    """
    session = read_option_session(underlying, trading_date, lake_root=lake_root)
    if session is None:
        return None, False
    underlying_day = LakeRunData(underlying, lake_root=lake_root).underlying_day(trading_date)
    return session, session.contracts_complete and underlying_day is not None


@dataclass(frozen=True)
class _Refusal:
    """Why one ticker's fetch stopped the capture; ``provider`` marks the vendor's own answer."""
    reason: OptionFailureReason
    detail: str
    provider: bool = False


def _receipt(payload: bytes, listing: OptionListing) -> dict[str, str]:
    """A staged outcome's receipt: its exact bytes, under this listing and recipe."""
    return {"sha256": hashlib.sha256(payload).hexdigest(), "listing_sha256": listing.sha256, "recipe": option_session_dch()}


async def _clear_committed_staging(
    generation: Path, result: OptionCaptureResult, *, staging_root: Path, budget: OptionSessionStaging,
) -> OptionCaptureResult:
    if result.failure is not None:
        return result
    generation = confine_path_to_root(generation, staging_root, label="option capture cleanup")
    if generation.is_dir():
        try:
            cleanup = asyncio.create_task(asyncio.to_thread(budget.clear_committed, generation))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.gather(cleanup, return_exceptions=True)
                raise
        except OSError as exc:
            logger.warning("committed option capture staging could not be cleared",
                           extra={"action": "option_capture_staging_cleanup_failed", "error_type": type(exc).__name__})
    return result


async def capture_option_session(
    underlying: str, trading_date: date, *, now_ms: int | None = None,
    should_pause: Callable[[], bool] | None = None, history_floor: date | None = None,
    retry_exhausted: bool = False, max_staging_bytes: int = DEFAULT_STAGING_MAX_BYTES,
) -> OptionCaptureResult:
    """Resolve every listed ticker. Repeated triggers join durable collection.

    Progress is retained on every pause: ``paused`` when ``should_pause`` asked for it,
    ``staging_capacity`` when the staging budget cannot hold the next outcome, and
    ``lease_timeout`` when another collector owns this session or a publication holds it.
    Explicit ``retry_exhausted`` grants one new bounded transient-attempt budget; normal
    scheduling never resets it.
    """
    if asyncio.get_running_loop() is not background_loop():
        future = asyncio.run_coroutine_threadsafe(capture_option_session(
            underlying, trading_date, now_ms=now_ms, should_pause=should_pause,
            history_floor=history_floor, retry_exhausted=retry_exhausted, max_staging_bytes=max_staging_bytes), background_loop())
        return await asyncio.wrap_future(future)
    await catalog_client.init_pool()
    now = current_ms() if now_ms is None else now_ms
    def failed(reason: OptionFailureReason, detail: str) -> OptionCaptureResult:
        return OptionCaptureResult.failed(OptionDataFailure(reason, detail, underlying, trading_date))
    refusal = refusal_before_claim(underlying, trading_date, now_ms=now)
    if refusal is not None:
        return OptionCaptureResult.failed(refusal)
    if trading_date < (history_floor or option_history_floor(et_date_at_ms(now))):
        return failed("outside_data_plan", "session is older than the archive fetch boundary")
    paused = should_pause or (lambda: False)
    staging_root = resolve_staging_root("raw")
    capture_root = confine_path_to_root(staging_root / "option_sessions", staging_root, label="option capture root")
    staging = confine_path_to_root(capture_root / underlying / str(session_open_ms_utc(trading_date)),
                                  capture_root, label="option capture session")
    collector = confine_path_to_root(staging / "collector", staging, label="option capture collector")
    confine_path_to_root(staging / ".collector.lock", staging, label="option capture collector lock")
    budget = OptionSessionStaging(staging_root, max_bytes=max_staging_bytes)
    with ExitStack() as held:
        try:
            owns = held.enter_context(budget.collector(collector))
        except StagingCapacityError as exc:
            return failed("staging_capacity", str(exc))
        if not owns:
            return failed("lease_timeout", "this session already has a running collector")
        if paused():
            return failed("paused", "capture paused before a provider request; progress retained")
        async with vendor_request_gate():
            listing_result = await ensure_option_listing(underlying, trading_date, now_ms=now)
        if listing_result.failure is not None:
            return listing_result
        listing = await asyncio.to_thread(read_option_listing, underlying, trading_date)
        if listing is None:
            return failed("listing_not_captured", "listing was not admitted after capture")
        generation = confine_path_to_root(staging / option_session_dch() / listing.sha256,
                                         staging, label="option capture generation")

        async def finish(outcomes: dict[str, bytes]) -> OptionCaptureResult:
            """Capture the underlying, then publish the container and clear its committed staging."""
            underlying_result = await capture_underlying(underlying, trading_date, now, allow_before_floor=history_floor is not None)
            if underlying_result.outcome is Outcome.FAILED:
                return failed(terminal_provider_reason(underlying_result.reason) or "provider_api_error",
                              underlying_result.detail or "underlying bars could not be captured")
            published = await publish_option_session(listing, outcomes, max_staging_bytes=max_staging_bytes)
            return await _clear_committed_staging(generation, published, staging_root=staging_root, budget=budget)

        try:
            existing = await asyncio.to_thread(read_option_session, underlying, trading_date, listing=listing)
        except LakeAdmissionError:
            if await complete_session_record(underlying, trading_date) is not None:
                raise
            existing = None
        if existing is not None and existing.contracts_complete:
            return await finish(dict(existing.original_bytes))
        generation = confine_path_to_root(generation, staging_root, label="option capture generation")
        generation.mkdir(parents=True, exist_ok=True)
        captured = {} if existing is None else dict(existing.original_bytes)
        for symbol, payload in (await asyncio.to_thread(admitted_legacy_members, listing)).items():
            captured.setdefault(symbol, payload)
        listed_symbols = frozenset(row.ticker.removeprefix("O:") for row in listing.rows)

        async def fetch(symbol: str, path: Path, receipt_path: Path, attempts_path: Path, attempts: int,
                        reservation: StagingReservation) -> _Refusal | None:
            """Fetch, validate and stage one ticker; a refusal is reported, never raised.

            A request that times out is asked again at once, up to ``TIMEOUT_TRIES_PER_REQUEST`` times, so one slow
            answer no longer fails the whole session until the next pass.
            """
            try:
                for tried in range(1, TIMEOUT_TRIES_PER_REQUEST + 1):
                    try:
                        async with vendor_request_gate():
                            if paused():
                                return _Refusal("paused", "capture paused before a provider request; progress retained")
                            bars = await fetch_listed_option_minute_bars(symbol, trading_date, settings.POLYGON_API_KEY,
                                                                       listed_tickers=listed_symbols)
                        break
                    except PolygonTimeoutError:
                        if tried == TIMEOUT_TRIES_PER_REQUEST:
                            raise
                        # The wait already lasted the whole request timeout, so the next try goes at once.
                        logger.warning("Option archive asked again for a contract that timed out", extra={
                            "action": "option_archive_request_timeout_retried", "underlying": underlying,
                            "symbol": symbol, "session_ms": session_open_ms_utc(trading_date), "try": tried})
                payload = encode_option_day(bars, symbol=symbol, trading_date=trading_date)
                check_member_size(payload)
                parse_option_day_zip(payload, symbol, trading_date)
            except PolygonFetchError as exc:
                reason = vendor_reason(exc)
                if reason != "outside_data_plan":
                    attempts_path = confine_path_to_root(attempts_path, staging_root, label="option capture attempts")
                    reservation.write({attempts_path: str(attempts + 1).encode()})
                return _Refusal(reason, str(exc), provider=True)
            except (CorruptVendorBarsError, ArithmeticError, ValueError) as exc:
                attempts_path = confine_path_to_root(attempts_path, staging_root, label="option capture attempts")
                reservation.write({attempts_path: str(attempts + 1).encode()})
                return _Refusal("validation_failed", str(exc))
            receipt_path = confine_path_to_root(receipt_path, staging_root, label="option capture receipt")
            path = confine_path_to_root(path, staging_root, label="option capture member")
            reservation.write({receipt_path: json.dumps(_receipt(payload, listing), sort_keys=True).encode(),
                               path: payload})
            captured[symbol] = payload
            return None

        # Up to the vendor gate's limit of tickers are in flight at once. Reservations are taken here, in listing
        # order, so a full staging budget stops the same tickers it did one at a time; a pause or refusal stops new
        # requests and lets those in flight finish. The container's members are sorted, so its bytes never depend on
        # which answer came first.
        in_flight: dict[asyncio.Task[_Refusal | None], int] = {}
        refusals: list[tuple[int, _Refusal]] = []
        stop: OptionCaptureResult | None = None

        async def settle(when: str) -> None:
            done, _ = await asyncio.wait(in_flight, return_when=when)
            for task in done:
                index = in_flight.pop(task)
                if (refusal := task.result()) is not None:
                    refusals.append((index, refusal))

        try:
            for index, row in enumerate(listing.rows):
                generation = confine_path_to_root(generation, staging_root, label="option capture generation")
                symbol = row.ticker.removeprefix("O:")
                # The parser rejects unsafe or mismatched symbols before URL or path use.
                try:
                    require_listed_option_symbol(symbol, listed_symbols)
                except ValueError as exc:
                    return failed("validation_failed", f"listed ticker cannot be captured: {exc}")
                path = confine_path_to_root(generation / f"{symbol}.zip", generation, label="option capture member")
                receipt_path = confine_path_to_root(generation / f"{symbol}.json", generation, label="option capture receipt")
                attempts_path = confine_path_to_root(generation / f"{symbol}.attempts", generation, label="option capture attempts")
                if path.is_file():
                    payload = path.read_bytes()
                    if json.loads(receipt_path.read_bytes()) != _receipt(payload, listing):
                        return failed("validation_failed", "staged ticker receipt does not match its exact bytes")
                    parse_option_day_zip(payload, symbol, trading_date)
                    captured.setdefault(symbol, payload)
                if symbol in captured:
                    continue
                while len(in_flight) >= MAX_VENDOR_REQUESTS_IN_FLIGHT and not refusals:
                    await settle(asyncio.FIRST_COMPLETED)
                if refusals:
                    break
                if paused():
                    stop = failed("paused", "capture paused; validated ticker outcomes retained")
                    break
                attempts = int(attempts_path.read_text()) if attempts_path.is_file() else 0
                if attempts >= MAX_CLAIM_RETRIES and not retry_exhausted:
                    stop = failed("fetch_timeout", "ticker exhausted its transient retries; explicit recovery is required")
                    break
                if retry_exhausted and attempts >= MAX_CLAIM_RETRIES:
                    attempts = 0
                reservation: StagingReservation | None = None
                while reservation is None:
                    try:
                        reservation = budget.reserve(TICKER_RESERVATION_BYTES)
                    except StagingCapacityError as exc:
                        # A budget too small for one more ticker beside those in flight waits for them; only one
                        # too small for a ticker on its own is full.
                        if not in_flight:
                            stop = failed("staging_capacity", str(exc))
                            break
                        await settle(asyncio.FIRST_COMPLETED)
                        if refusals:
                            break
                if reservation is None:
                    break
                task = asyncio.create_task(fetch(symbol, path, receipt_path, attempts_path, attempts, reservation))
                # A callback, not the coroutine's own finally: a task cancelled before it starts never runs one.
                task.add_done_callback(lambda _, held=reservation: held.close())
                in_flight[task] = index
            while in_flight:
                await settle(asyncio.ALL_COMPLETED)
        except BaseException:
            for task in in_flight:
                task.cancel()
            raise
        finally:
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        if refusals:
            # The first refusal in listing order is the one a ticker-by-ticker capture would have reported.
            _, refusal = min(refusals, key=lambda item: item[0])
            if refusal.provider and captured and refusal.reason in TERMINAL_PROVIDER_REASONS:
                partial = await publish_option_session(listing, captured, max_staging_bytes=max_staging_bytes)
                if partial.failure is not None:
                    if partial.failure.reason == "staging_capacity":
                        return failed(refusal.reason, f"{refusal.detail}; partial publication paused: {partial.failure.detail}")
                    return partial
            return failed(refusal.reason, refusal.detail)
        if stop is not None:
            return stop
        if paused():
            return failed("paused", "capture paused before underlying collection; outcomes retained")
        return await finish(captured)
