"""Resumable full-listing session capture with brief publication leases (#3427).

A same-host advisory lock fences the collector for its whole lifetime; process
death releases it. Validated ticker outcomes are fsynced independently under
the listing/recipe identity. There is no catalog lease during provider work,
no staging cleanup by a losing collector, and no staged coverage is readable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.bar_validation import CorruptVendorBarsError, assert_publishable_minute_bars
from app.data_lake.lean_writer import build_minute_trade_zip_bytes
from app.data_lake.option_bars import (
    _minute_trade_bar,
    _publication_gate_view,
    _require_exact_price_relations,
    _require_prices_above_zero_on_the_grid,
    _require_whole_minute_starts,
    parse_option_day_zip,
)
from app.data_lake.option_lake import (
    MAX_CLAIM_RETRIES,
    OptionCaptureResult,
    OptionDataFailure,
    OptionFailureReason,
    current_ms,
    option_history_floor,
    refusal_before_claim,
    vendor_reason,
    vendor_request_gate,
)
from app.data_lake.option_listing import ensure_option_listing, read_option_listing
from app.data_lake.option_session import _check_inner_size, option_session_dch, read_option_session
from app.data_lake.option_session_repack import _complete, admitted_legacy_members, publish_option_session
from app.data_lake.option_session_staging import (
    CAPACITY_PREFIX,
    DEFAULT_STAGING_MAX_BYTES,
    TICKER_RESERVATION_BYTES,
    OptionSessionStaging,
    StagingCapacityError,
)
from app.data_lake.path_policy import resolve_staging_root
from app.data_lake.polygon_fetcher import PolygonFetchError
from app.data_lake.polygon_option_bars import fetch_listed_option_minute_bars, require_listed_option_symbol
from app.engine.live.identity import confine_path_to_root
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options_backtest.chain_download import Outcome, _capture_underlying
from app.utils.background_loop import background_loop
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)


async def _clear_committed_staging(
    generation: Path, result: OptionCaptureResult, *, staging_root: Path, budget: OptionSessionStaging | None = None,
) -> OptionCaptureResult:
    if result.failure is not None:
        return result
    generation = confine_path_to_root(generation, staging_root, label="option capture cleanup")
    if generation.is_dir():
        try:
            cleanup = asyncio.create_task(asyncio.to_thread(
                (budget or OptionSessionStaging(staging_root)).clear_committed, generation))
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

    A ``lease_timeout`` result means another collector owns this session, or a
    deliberate pause retained its progress. Explicit ``retry_exhausted`` grants
    one new bounded transient-attempt budget; normal scheduling never resets it.
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
    with budget.collector(collector) as owns:
        if not owns:
            return failed("lease_timeout", budget.pause_reason or "this session already has a running collector")
        if paused():
            return failed("lease_timeout", "capture paused before a provider request; progress retained")
        async with vendor_request_gate():
            listing_result = await ensure_option_listing(underlying, trading_date, now_ms=now)
        if listing_result.failure is not None:
            return listing_result
        listing = await asyncio.to_thread(read_option_listing, underlying, trading_date)
        if listing is None:
            return failed("listing_not_captured", "listing was not admitted after capture")
        generation = confine_path_to_root(staging / option_session_dch() / listing.sha256,
                                         staging, label="option capture generation")
        try:
            existing = await asyncio.to_thread(read_option_session, underlying, trading_date, listing=listing)
        except LakeAdmissionError:
            if await _complete(underlying, trading_date) is not None:
                raise
            existing = None
        if existing is not None and existing.contracts_complete:
            underlying_result = await _capture_underlying(underlying, trading_date, now, allow_before_floor=history_floor is not None)
            if underlying_result.outcome is Outcome.FAILED:
                reason: OptionFailureReason = {
                    "provider_auth_error": "provider_auth_error", "provider_entitlement_error": "provider_entitlement_error",
                    "outside_data_plan": "outside_data_plan",
                }.get(underlying_result.reason, "provider_api_error")
                return failed(reason, underlying_result.detail or "underlying bars could not be captured")
            return await _clear_committed_staging(generation, await publish_option_session(listing, dict(existing.original_bytes), max_staging_bytes=max_staging_bytes),
                                                  staging_root=staging_root, budget=budget)
        generation = confine_path_to_root(generation, staging_root, label="option capture generation")
        generation.mkdir(parents=True, exist_ok=True)
        captured = {} if existing is None else dict(existing.original_bytes)
        for symbol, payload in (await asyncio.to_thread(admitted_legacy_members, listing)).items():
            captured.setdefault(symbol, payload)
        listed_symbols = frozenset(row.ticker.removeprefix("O:") for row in listing.rows)
        for row in listing.rows:
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
                receipt = json.loads(receipt_path.read_bytes())
                if receipt != {"sha256": hashlib.sha256(payload).hexdigest(), "listing_sha256": listing.sha256,
                               "recipe": option_session_dch()}:
                    return failed("validation_failed", "staged ticker receipt does not match its exact bytes")
                parse_option_day_zip(payload, symbol, trading_date)
                captured.setdefault(symbol, payload)
            if symbol in captured:
                continue
            if paused():
                return failed("lease_timeout", "capture paused; validated ticker outcomes retained")
            attempts = int(attempts_path.read_text()) if attempts_path.is_file() else 0
            if attempts >= MAX_CLAIM_RETRIES and not retry_exhausted:
                return failed("fetch_timeout", "ticker exhausted its transient retries; explicit recovery is required")
            if retry_exhausted and attempts >= MAX_CLAIM_RETRIES:
                attempts = 0
            try:
                reservation = budget.reserve(TICKER_RESERVATION_BYTES)
            except StagingCapacityError as exc:
                return failed("lease_timeout", str(exc))
            try:
                try:
                    async with vendor_request_gate():
                        if paused():
                            return failed("lease_timeout", "capture paused before a provider request; progress retained")
                        bars = await fetch_listed_option_minute_bars(symbol, trading_date, settings.POLYGON_API_KEY,
                                                                   listed_tickers=listed_symbols)
                    _require_exact_price_relations(bars, symbol=symbol, trading_date=trading_date)
                    assert_publishable_minute_bars(_publication_gate_view(bars), symbol=symbol, trading_date=trading_date)
                    _require_whole_minute_starts([bar.t_ms for bar in bars], symbol=symbol, trading_date=trading_date)
                    _require_prices_above_zero_on_the_grid(bars)
                    payload = build_minute_trade_zip_bytes(symbol=symbol, trading_date_yyyymmdd=f"{trading_date:%Y%m%d}",
                                                           bars=[_minute_trade_bar(bar) for bar in bars])
                    _check_inner_size(payload)
                    parse_option_day_zip(payload, symbol, trading_date)
                except PolygonFetchError as exc:
                    reason = vendor_reason(exc)
                    if reason != "outside_data_plan":
                        attempts_path = confine_path_to_root(attempts_path, staging_root, label="option capture attempts")
                        reservation.write({attempts_path: str(attempts + 1).encode()})
                    if captured and reason in {"outside_data_plan", "provider_auth_error", "provider_entitlement_error"}:
                        partial = await publish_option_session(listing, captured, max_staging_bytes=max_staging_bytes)
                        if partial.failure is not None:
                            if (partial.failure.reason == "lease_timeout"
                                    and partial.failure.detail.startswith(CAPACITY_PREFIX)):
                                return failed(reason, f"{exc}; partial publication paused: {partial.failure.detail}")
                            return partial
                    return failed(reason, str(exc))
                except (CorruptVendorBarsError, ArithmeticError, ValueError) as exc:
                    attempts_path = confine_path_to_root(attempts_path, staging_root, label="option capture attempts")
                    reservation.write({attempts_path: str(attempts + 1).encode()})
                    return failed("validation_failed", str(exc))
                receipt_path = confine_path_to_root(receipt_path, staging_root, label="option capture receipt")
                path = confine_path_to_root(path, staging_root, label="option capture member")
                reservation.write({receipt_path: json.dumps({"sha256": hashlib.sha256(payload).hexdigest(),
                    "listing_sha256": listing.sha256, "recipe": option_session_dch()}, sort_keys=True).encode(), path: payload})
                captured[symbol] = payload
            finally:
                reservation.close()
        if paused():
            return failed("lease_timeout", "capture paused before underlying collection; outcomes retained")
        underlying_result = await _capture_underlying(underlying, trading_date, now, allow_before_floor=history_floor is not None)
        if underlying_result.outcome is Outcome.FAILED:
            reason = {
                "provider_auth_error": "provider_auth_error",
                "provider_entitlement_error": "provider_entitlement_error",
                "outside_data_plan": "outside_data_plan",
            }.get(underlying_result.reason, "provider_api_error")
            return failed(reason, underlying_result.detail or "underlying bars could not be captured")
        return await _clear_committed_staging(generation, await publish_option_session(listing, captured, max_staging_bytes=max_staging_bytes),
                                              staging_root=staging_root, budget=budget)
