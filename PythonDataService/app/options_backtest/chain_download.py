"""Downloading into the data lake what the past chain needs to show a date (#3030).

A chain needs three things of one trading date: the listing of the underlying's contracts, the underlying's own raw
minute bars (for its price, and so the money), and the minute bars of the contracts the owner wants prices for. Each is
captured through the lake's own function and only through it: the listing by ``ensure_option_listing``, a contract's
day by ``ensure_option_day_bars`` (handed the listing already read, so it is read once), and the underlying by
``materialize_engine_run``, which the stock engine uses. The lake decides what to ask the vendor: a thing it holds is
reused with no request, and this module asks for nothing twice.

Every outcome is one item and none is raised: ``fetched`` (the lake fetched it now), ``already_stored`` (the lake
had it) or ``failed`` with the lake's own typed reason. A contract is refused by the lake, with no request, when the
listing does not hold it, so a listing that could not be captured fails every contract with ``listing_not_captured``
and no contract is tried. What is raised is the caller's mistake (a request refused before anything is asked) and an
unreachable catalog.

At most ``MAX_REQUESTS_IN_FLIGHT`` captures run at once in one call, and at most ``MAX_CONTRACTS_PER_DOWNLOAD``
contracts are taken in one call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_bars import ensure_option_day_bars
from app.data_lake.option_lake import OptionCaptureResult, OptionFailureReason
from app.data_lake.option_listing import (
    OptionListing,
    OptionListingFileError,
    ensure_option_listing,
    read_option_listing,
)
from app.data_lake.run_materialization import LakeMaterializationError, materialize_engine_run
from app.data_lake.types import is_lake_addressable_symbol
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms
from app.options_backtest.lake_data import LakeRunData, UnderlyingDayUnreadable
from app.options_backtest.past_chain import ChainRefusal
from app.utils.background_loop import CallerStoppedWaitingError

# Vendor requests in flight at once in one call. The listing and the underlying each count as one.
MAX_REQUESTS_IN_FLIGHT = 4
# Contracts one call takes. A full window of the chain (58 contracts at the widest) fits in one call.
MAX_CONTRACTS_PER_DOWNLOAD = 60

DownloadReason = OptionFailureReason | Literal["underlying_not_fetched", "underlying_unreadable"]


class TooManyContracts(ChainRefusal):
    code = "OPTIONS_BACKTEST_TOO_MANY_CONTRACTS"


class ContractNotOfExpiry(ChainRefusal):
    code = "OPTIONS_BACKTEST_CONTRACT_NOT_OF_EXPIRY"


class ItemKind(StrEnum):
    LISTING = "listing"
    UNDERLYING = "underlying"
    CONTRACT = "contract"


class Outcome(StrEnum):
    FETCHED = "fetched"
    ALREADY_STORED = "already_stored"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadItem:
    kind: ItemKind
    # The underlying's ticker, or the contract's OCC symbol.
    subject: str
    outcome: Outcome
    # The lake's typed reason (or one of the two for the underlying), and its words; both None unless it failed.
    reason: DownloadReason | None
    detail: str | None


def _item_of(kind: ItemKind, subject: str, result: OptionCaptureResult) -> DownloadItem:
    if result.failure is not None:
        return DownloadItem(kind, subject, Outcome.FAILED, result.failure.reason, result.failure.detail)
    return DownloadItem(kind, subject, Outcome.ALREADY_STORED if result.reused else Outcome.FETCHED, None, None)


async def _capture_listing(
    underlying: str, trading_date: date, gate: asyncio.Semaphore, now_ms: int | None
) -> tuple[DownloadItem, OptionListing | None]:
    """The listing, captured or reused, and read once so each contract capture can be handed it."""
    async with gate:
        result = await ensure_option_listing(underlying, trading_date, now_ms=now_ms)
    item = _item_of(ItemKind.LISTING, underlying, result)
    if item.outcome is Outcome.FAILED:
        return item, None
    try:
        stored = await asyncio.to_thread(read_option_listing, underlying, trading_date)
        why = "the lake no longer holds the file it just reported"
    except (LakeAdmissionError, OptionListingFileError) as exc:
        stored = None
        why = f"{type(exc).__name__}: {exc}"
    if stored is None:
        detail = f"the {underlying} listing for {trading_date.isoformat()} is stored but is not usable: {why}"
        return DownloadItem(ItemKind.LISTING, underlying, Outcome.FAILED, "listing_not_captured", detail), None
    return item, stored


async def _capture_underlying(underlying: str, trading_date: date, gate: asyncio.Semaphore) -> DownloadItem:
    """The underlying's raw minute bars: left alone when held, fetched by the stock engine's own function when not."""
    held_by = LakeRunData(underlying)
    try:
        held = await asyncio.to_thread(held_by.underlying_day, trading_date)
    except (LakeAdmissionError, UnderlyingDayUnreadable) as exc:
        # Repairing a damaged stock file is the stock side's job; nothing here knows the pipeline would heal it.
        return DownloadItem(
            ItemKind.UNDERLYING,
            underlying,
            Outcome.FAILED,
            "underlying_unreadable",
            f"{type(exc).__name__}: {exc}",
        )
    if held is not None:
        return DownloadItem(ItemKind.UNDERLYING, underlying, Outcome.ALREADY_STORED, None, None)
    async with gate:
        try:
            await asyncio.to_thread(
                materialize_engine_run,
                symbol=underlying,
                start=trading_date,
                end=trading_date,
                resolution="minute",
                price_adjustment_mode="raw",
            )
        except (LakeMaterializationError, CallerStoppedWaitingError) as exc:
            return DownloadItem(ItemKind.UNDERLYING, underlying, Outcome.FAILED, "underlying_not_fetched", str(exc))
    return DownloadItem(ItemKind.UNDERLYING, underlying, Outcome.FETCHED, None, None)


async def _capture_contract(
    contract: OptionContract,
    underlying: str,
    trading_date: date,
    listing: OptionListing,
    gate: asyncio.Semaphore,
    now_ms: int | None,
) -> DownloadItem:
    async with gate:
        result = await ensure_option_day_bars(
            contract, trading_date, listed_under=underlying, listing=listing, now_ms=now_ms
        )
    return _item_of(ItemKind.CONTRACT, contract.occ_symbol, result)


def _distinct(contracts: Sequence[OptionContract]) -> list[OptionContract]:
    """The contracts with a repeat dropped, in the order they were first named: nothing is asked twice."""
    first: dict[str, OptionContract] = {}
    for contract in contracts:
        first.setdefault(contract.occ_symbol, contract)
    return list(first.values())


def _raise_the_first_error(outcomes: Sequence[object]) -> None:
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


async def ensure_chain_data(
    underlying: str,
    trading_date: date,
    *,
    expiry: date | None,
    contracts: Sequence[OptionContract],
    now_ms: int | None = None,
) -> tuple[DownloadItem, ...]:
    """Capture the listing of ``trading_date``, the underlying's bars, and the bars of each named contract.

    The items come in a fixed order: the listing, the underlying, then the contracts as named. ``expiry``, when
    given, is a check that every contract expires on it (``ContractNotOfExpiry`` if one does not); it captures nothing
    of its own. More than ``MAX_CONTRACTS_PER_DOWNLOAD`` distinct contracts is ``TooManyContracts``. Both refusals
    come before anything is asked. Raises ``catalog_client.CatalogUnavailableError`` when the catalog cannot be
    reached, after the captures in flight have finished, and ``ValueError`` for an underlying the lake cannot hold.
    """
    named = _distinct(contracts)
    if len(named) > MAX_CONTRACTS_PER_DOWNLOAD:
        raise TooManyContracts(
            f"a download takes at most {MAX_CONTRACTS_PER_DOWNLOAD} contracts; {len(named)} were named"
        )
    if expiry is not None:
        anchor = option_expiry_anchor_ms(expiry)
        strangers = [contract.occ_symbol for contract in named if contract.expiry_ms != anchor]
        if strangers:
            raise ContractNotOfExpiry(f"these contracts do not expire on {expiry.isoformat()}: {', '.join(strangers)}")
    if not is_lake_addressable_symbol(underlying):
        raise ValueError(f"the underlying must be an upper-case ticker the lake can hold, got {underlying!r}")
    # The catalog's pool belongs to the loop that made it, and the lake's capture functions do not make one.
    await catalog_client.init_pool()
    gate = asyncio.Semaphore(MAX_REQUESTS_IN_FLIGHT)
    # Every capture finishes before an error is raised: none is cancelled halfway through a claim.
    first = await asyncio.gather(
        _capture_listing(underlying, trading_date, gate, now_ms),
        _capture_underlying(underlying, trading_date, gate),
        return_exceptions=True,
    )
    _raise_the_first_error(first)
    (listing_item, listing), underlying_item = first  # type: ignore[misc]
    if listing is None:
        why = f"the {underlying} listing for {trading_date.isoformat()} could not be captured ({listing_item.reason})"
        return (
            listing_item,
            underlying_item,
            *(
                DownloadItem(
                    ItemKind.CONTRACT,
                    contract.occ_symbol,
                    Outcome.FAILED,
                    "listing_not_captured",
                    f"{why}, so no contract of it can be vouched for",
                )
                for contract in named
            ),
        )
    contract_outcomes = await asyncio.gather(
        *(_capture_contract(contract, underlying, trading_date, listing, gate, now_ms) for contract in named),
        return_exceptions=True,
    )
    _raise_the_first_error(contract_outcomes)
    return (listing_item, underlying_item, *(item for item in contract_outcomes if isinstance(item, DownloadItem)))
