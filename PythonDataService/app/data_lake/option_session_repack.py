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
    option_session_publication_lock,
    publish_option_artifact,
    stored_file_still_reads,
)
from app.data_lake.option_listing import OptionListing, read_option_listing
from app.data_lake.option_session import (
    build_option_session_bytes,
    option_session_dch,
    parse_option_session,
    read_option_session,
    read_option_session_for_listing_rebind,
)
from app.data_lake.option_session_staging import DEFAULT_STAGING_MAX_BYTES, OptionSessionStaging, StagingCapacityError
from app.data_lake.path_policy import OptionMinuteBarPath, OptionSessionPath, resolve_staging_root
from app.data_lake.types import ArtifactIdentity, ArtifactRecord
from app.options.contract import OptionContract


def admitted_legacy_members(listing: OptionListing, *, only_symbols: frozenset[str] | None = None) -> dict[str, bytes]:
    """Read only existing admitted sources; a missing ticker remains unresolved."""
    root = option_read_root()
    result: dict[str, bytes] = {}
    for row in listing.rows:
        symbol = row.ticker.removeprefix("O:")
        if only_symbols is not None and symbol not in only_symbols:
            continue
        try:
            contract = OptionContract.from_occ_symbol(symbol)
        except ValueError:
            # An unparseable listing row has no legacy artifact; it remains unresolved.
            continue
        path = root / OptionMinuteBarPath(contract.root, symbol, listing.as_of).relative_path()
        if path.is_file():
            result[symbol] = read_committed_bytes(path)
    return result


async def complete_session_record(underlying: str, trading_date: date) -> ArtifactRecord | None:
    """The catalog's complete row of the session container under this recipe, if there is one."""
    rows = await catalog_client.select_coverage_minute_bars(
        market=OPTION_MARKET, symbol=underlying, data_type="trade", start_trading_date=trading_date,
        end_trading_date=trading_date, price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
    )
    return next((row for row in rows if row.provider == OPTION_PROVIDER and row.data_contract_hash == option_session_dch()), None)


async def publish_option_session(listing: OptionListing, captured: dict[str, bytes], *,
                                 max_staging_bytes: int = DEFAULT_STAGING_MAX_BYTES) -> OptionCaptureResult:
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
        return await _publish_option_session(listing, captured, max_staging_bytes=max_staging_bytes)


async def _publish_option_session(listing: OptionListing, captured: dict[str, bytes], *, rebind_listing: bool = False,
                                  max_staging_bytes: int = DEFAULT_STAGING_MAX_BYTES) -> OptionCaptureResult:
    underlying, day = listing.underlying, listing.as_of
    rel_path = OptionSessionPath(underlying, day).relative_path()
    identity = ArtifactIdentity(artifact_kind="time_series_bars", market=OPTION_MARKET, symbol=underlying,
                                trading_date=day, resolution="minute", data_type="trade", provider=OPTION_PROVIDER,
                                price_adjustment_mode="raw")
    try:
        previous = (await asyncio.to_thread(read_option_session_for_listing_rebind, underlying, day) if rebind_listing
                    else await asyncio.to_thread(read_option_session, underlying, day, listing=listing))
    except LakeAdmissionError:
        if await complete_session_record(underlying, day) is not None:
            raise
        # A crash after rename but before receipt commit left unreadable bytes.
        # Claim/reclaim below still decides whether this writer can replace them.
        previous = None
    listed = {row.ticker.removeprefix("O:") for row in listing.rows}
    merged = {symbol: raw for symbol, raw in captured.items() if symbol in listed}
    unresolved = frozenset(listed - (set() if previous is None else set(previous.days)))
    # Resolve stale incoming snapshots against today's admitted legacy receipt
    # while every normal source publisher is excluded by this same lock.
    merged.update(await asyncio.to_thread(admitted_legacy_members, listing, only_symbols=unresolved))
    if previous is not None:
        # Admitted container outcomes are immutable. A late capture, old staging,
        # or retained legacy file may only fill a still-unresolved ticker.
        merged.update({symbol: raw for symbol, raw in previous.original_bytes.items() if symbol in listed})
    payload = await asyncio.to_thread(build_option_session_bytes, listing, merged)
    digest = hashlib.sha256(payload).hexdigest()
    recipe = option_session_dch()

    async def claim() -> int | None:
        return await catalog_client.claim_minute_bar(identity=identity, worker_id=WORKER_ID,
            lease_ttl_ms=LEASE_TTL_MS, data_contract_hash=recipe, file_path=str(rel_path))

    async def matches(record: ArtifactRecord) -> bool:
        return record.file_sha256 == digest and await stored_file_still_reads(record, rel_path,
            lambda raw: parse_option_session(raw, listing))

    budget = OptionSessionStaging(resolve_staging_root("raw"), max_bytes=max_staging_bytes)
    try:
        reservation = budget.reserve(len(payload))
    except StagingCapacityError as exc:
        return OptionCaptureResult.failed(OptionDataFailure("staging_capacity", str(exc), underlying, day))
    try:
        held = await claim_or_take_over(claim=claim, select_complete=lambda: complete_session_record(underlying, day),
            read_claim_state=lambda: catalog_client.select_minute_bar_claim_state(identity), stored_file_reads=matches,
            data_contract_hash=recipe, symbol=underlying, trading_date=day)
        if isinstance(held, OptionCaptureResult):
            return held
        return await _publish(held, identity, listing, payload, staging_budget=budget)
    finally:
        reservation.close(reconcile=True)


async def _publish(lease: HeldLease, identity: ArtifactIdentity, listing: OptionListing, payload: bytes, *,
                   staging_budget: OptionSessionStaging) -> OptionCaptureResult:
    """Re-admit listing immediately before publication; do not publish stale identity."""
    current = await asyncio.to_thread(read_option_listing, listing.underlying, listing.as_of)
    if current is None or current.sha256 != listing.sha256:
        return await fail_row(artifact_id=lease.artifact_id, lease_generation=lease.lease_generation,
            reason="validation_failed", detail="listing changed during repack", symbol=listing.underlying, trading_date=listing.as_of)
    parsed = parse_option_session(payload, listing)
    rel_path = OptionSessionPath(listing.underlying, listing.as_of).relative_path()
    digest = await publish_option_artifact(payload=payload, rel_path=rel_path, lease=lease,
        row_count=parsed.answered_count, first_bar_start_ms=0, last_bar_start_ms=0,
        data_contract_hash=option_session_dch(), symbol=listing.underlying, trading_date=listing.as_of,
        staging_root_override=staging_budget.publication_root)
    if isinstance(digest, OptionDataFailure):
        return OptionCaptureResult.failed(digest)
    record = ArtifactRecord(id=lease.artifact_id, **identity.model_dump(), data_contract_hash=option_session_dch(),
                            file_path=str(rel_path), file_sha256=digest, row_count=parsed.answered_count,
                            first_bar_start_ms=0, last_bar_start_ms=0, file_size_bytes=len(payload))
    admitted = await asyncio.to_thread(read_option_session, listing.underlying, listing.as_of)
    if admitted is None or admitted.sha256 != digest:
        raise LakeAdmissionError("published session did not read through the production reader")
    return OptionCaptureResult(record=record, failure=None)


async def repack_option_session(underlying: str, trading_date: date, *, rebind_listing: bool = False,
                                max_staging_bytes: int = DEFAULT_STAGING_MAX_BYTES) -> OptionCaptureResult:
    """No vendor access; source admission or parsing errors stop the repack."""
    with option_session_publication_lock(underlying, trading_date) as owns:
        if not owns:
            return OptionCaptureResult.failed(OptionDataFailure("lease_timeout", "a source capture or repack is publishing; retry", underlying, trading_date))
        listing = await asyncio.to_thread(read_option_listing, underlying, trading_date)
        if listing is None:
            return OptionCaptureResult.failed(OptionDataFailure("listing_not_captured", "stored listing is missing", underlying, trading_date))
        # Every source publisher takes this same lock, so the sources cannot change while it is held.
        captured = await asyncio.to_thread(admitted_legacy_members, listing)
        return await _publish_option_session(listing, captured, rebind_listing=rebind_listing, max_staging_bytes=max_staging_bytes)
