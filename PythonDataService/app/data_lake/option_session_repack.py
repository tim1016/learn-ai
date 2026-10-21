"""Offline option-day compaction with admitted exact source bytes (ADR 0078)."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date

from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.option_lake import (
    LEASE_TTL_MS,
    OPTION_MARKET,
    OPTION_PRICE_ADJUSTMENT_MODE,
    OPTION_PROVIDER,
    WORKER_ID,
    HeldLease,
    OptionCaptureResult,
    OptionDataFailure,
    claim_or_take_over,
    fail_row,
    option_read_root,
    publish_option_artifact,
    stored_file_still_reads,
)
from app.data_lake.option_listing import OptionListing, read_option_listing
from app.data_lake.option_session import (
    build_option_session_bytes,
    option_session_dch,
    option_session_publication_lock,
    parse_option_session,
    read_option_session,
)
from app.data_lake.path_policy import OptionMinuteBarPath, OptionSessionPath
from app.data_lake.types import ArtifactIdentity, ArtifactRecord
from app.options.contract import OptionContract


def admitted_legacy_members(listing: OptionListing) -> dict[str, bytes]:
    """Read only existing admitted sources; a missing ticker remains unresolved."""
    root = option_read_root()
    result: dict[str, bytes] = {}
    for row in listing.rows:
        symbol = row.ticker.removeprefix("O:")
        try:
            contract = OptionContract.from_occ_symbol(symbol)
        except ValueError:
            # An unparseable listing row has no legacy artifact; it remains unresolved.
            continue
        path = root / OptionMinuteBarPath(contract.root, symbol, listing.as_of).relative_path()
        if path.is_file():
            result[symbol] = read_committed_bytes(path)
    return result


async def _complete(underlying: str, trading_date: date) -> ArtifactRecord | None:
    rows = await catalog_client.select_coverage_minute_bars(
        market=OPTION_MARKET, symbol=underlying, data_type="trade", start_trading_date=trading_date,
        end_trading_date=trading_date, price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
    )
    return next((row for row in rows if row.provider == OPTION_PROVIDER and row.data_contract_hash == option_session_dch()), None)


async def publish_option_session(listing: OptionListing, captured: dict[str, bytes]) -> OptionCaptureResult:
    """Merge, fence and publish one container. Collection never holds this lease.

    Existing admitted members are unioned with new outcomes. An older repack can
    never downgrade already-resolved coverage. Original per-contract files are
    preserved, so a capture arriving after this snapshot stays readable and a
    repeated repack incorporates it without a provider request.
    """
    with option_session_publication_lock(listing.underlying, listing.as_of) as owns:
        if not owns:
            return OptionCaptureResult.failed(OptionDataFailure("lease_timeout", "session publication is busy; retry",
                                                                listing.underlying, listing.as_of))
        return await _publish_option_session(listing, captured)


async def _publish_option_session(listing: OptionListing, captured: dict[str, bytes]) -> OptionCaptureResult:
    underlying, day = listing.underlying, listing.as_of
    rel_path = OptionSessionPath(underlying, day).relative_path()
    identity = ArtifactIdentity(artifact_kind="time_series_bars", market=OPTION_MARKET, symbol=underlying,
                                trading_date=day, resolution="minute", data_type="trade", provider=OPTION_PROVIDER,
                                price_adjustment_mode="raw")
    try:
        previous = await asyncio.to_thread(read_option_session, underlying, day, listing=listing)
    except LakeAdmissionError:
        if await _complete(underlying, day) is not None:
            raise
        # A crash after rename but before receipt commit left unreadable bytes.
        # Claim/reclaim below still decides whether this writer can replace them.
        previous = None
    merged = {} if previous is None else dict(previous.original_bytes)
    merged.update(captured)
    payload = await asyncio.to_thread(build_option_session_bytes, listing, merged)
    digest = hashlib.sha256(payload).hexdigest()
    recipe = option_session_dch()

    async def claim() -> int | None:
        return await catalog_client.claim_minute_bar(identity=identity, worker_id=WORKER_ID,
            lease_ttl_ms=LEASE_TTL_MS, data_contract_hash=recipe, file_path=str(rel_path))

    async def matches(record: ArtifactRecord) -> bool:
        return record.file_sha256 == digest and await stored_file_still_reads(record, rel_path,
            lambda raw: parse_option_session(raw, listing))

    held = await claim_or_take_over(claim=claim, select_complete=lambda: _complete(underlying, day),
        read_claim_state=lambda: catalog_client.select_minute_bar_claim_state(identity), stored_file_reads=matches,
        data_contract_hash=recipe, symbol=underlying, trading_date=day)
    if isinstance(held, OptionCaptureResult):
        return held
    return await _publish(held, identity, listing, payload)


async def _publish(lease: HeldLease, identity: ArtifactIdentity, listing: OptionListing, payload: bytes) -> OptionCaptureResult:
    """Re-admit listing immediately before publication; do not publish stale identity."""
    current = await asyncio.to_thread(read_option_listing, listing.underlying, listing.as_of)
    if current is None or current.sha256 != listing.sha256:
        return await fail_row(artifact_id=lease.artifact_id, lease_generation=lease.lease_generation,
            reason="validation_failed", detail="listing changed during repack", symbol=listing.underlying, trading_date=listing.as_of)
    parsed = parse_option_session(payload, listing)
    rel_path = OptionSessionPath(listing.underlying, listing.as_of).relative_path()
    digest = await publish_option_artifact(payload=payload, rel_path=rel_path, lease=lease,
        row_count=parsed.answered_count, first_bar_start_ms=0, last_bar_start_ms=0,
        data_contract_hash=option_session_dch(), symbol=listing.underlying, trading_date=listing.as_of)
    if isinstance(digest, OptionDataFailure):
        return OptionCaptureResult.failed(digest)
    record = ArtifactRecord(id=lease.artifact_id, **identity.model_dump(), data_contract_hash=option_session_dch(),
                            file_path=str(rel_path), file_sha256=digest, row_count=parsed.answered_count,
                            first_bar_start_ms=0, last_bar_start_ms=0, file_size_bytes=len(payload))
    admitted = await asyncio.to_thread(read_option_session, listing.underlying, listing.as_of)
    if admitted is None or admitted.sha256 != digest:
        raise LakeAdmissionError("published session did not read through the production reader")
    return OptionCaptureResult(record=record, failure=None)


async def repack_option_session(underlying: str, trading_date: date) -> OptionCaptureResult:
    """No vendor access; source admission or parsing errors stop the repack."""
    with option_session_publication_lock(underlying, trading_date) as owns:
        if not owns:
            return OptionCaptureResult.failed(OptionDataFailure("lease_timeout", "a source capture or repack is publishing; retry", underlying, trading_date))
        listing = await asyncio.to_thread(read_option_listing, underlying, trading_date)
        if listing is None:
            return OptionCaptureResult.failed(OptionDataFailure("listing_not_captured", "stored listing is missing", underlying, trading_date))
        captured = await asyncio.to_thread(admitted_legacy_members, listing)
        # Recheck source receipts and bytes after collecting the last source, before
        # taking the publication lease. Refreshes cannot silently become empty proof.
        current = await asyncio.to_thread(admitted_legacy_members, listing)
        for symbol, raw in captured.items():
            if current.get(symbol) != raw:
                return OptionCaptureResult.failed(OptionDataFailure("validation_failed", "source changed during repack", underlying, trading_date))
        return await _publish_option_session(listing, current)
