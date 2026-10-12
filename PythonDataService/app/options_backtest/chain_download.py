"""Downloading into the data lake what the past chain needs to show a date (#3030).

A chain needs three things of one trading date: the listing of the underlying's contracts, the underlying's own raw
minute bars (for its price, and so the money), and the minute bars of the contracts the owner wants prices for. Each is
captured through the lake's own function and only through it: the listing by ``ensure_option_listing``, a contract's
day by ``ensure_option_day_bars`` (handed the listing already read, so it is read once), and the underlying by
``materialize_engine_run``, which the stock engine uses. The lake decides what to ask the vendor: a thing it holds is
reused with no request, and this module asks for nothing twice.

The stock pipeline has no gate on a session that is not over: asked for today's bars it stores the minutes so far as
the whole day, and every later read takes them as that. So the underlying's bars are fetched only for a session the
lake's own rule (``option_lake.session_refusal``) lets through, the one it applies to the listing and to a contract.

Every outcome is one item and none is raised: ``fetched`` (the lake fetched it now), ``already_stored`` (the lake
had it) or ``failed`` with the lake's own typed reason. A contract is refused by the lake, with no request, when the
listing does not hold it, so a listing that could not be captured fails every contract with ``listing_not_captured``
and no contract is tried. What is raised is the caller's mistake (a request refused before anything is asked) and an
unreachable catalog.

Every capture holds ``option_lake.vendor_request_gate`` while it asks: the one limit of
``option_lake.MAX_VENDOR_REQUESTS_IN_FLIGHT`` requests in flight for the whole process, which a run's download holds
too, so a chain download and a run at once do not double it. That gate is one semaphore per event loop, and the
process's background loop is where a run's download asks, so the captures are handed to that loop and not run on the
caller's.

A caller that cancels the download does not stop a capture that has begun. A capture that has claimed its catalog row
must reach its publish or its failure, or the row stays ``fetching`` until its lease runs out; and a task that awaits
work on another loop and is cancelled cancels that work. So the cancellation is held at the caller (``_waited_for``):
the caller keeps waiting until every capture it began has ended, begins no more, and only then is cancelled.

At most ``MAX_CONTRACTS_PER_DOWNLOAD`` contracts are taken in one call. That is the number of contracts a window of the
chain can hold (``past_chain.MAX_WINDOW_CONTRACTS``, one constant), so the contracts a window names are one download.

Two calls at once for one date can both want a contract. The lake lets one of them claim it, and the other waits out its
lease and gives up: that call reports the contract ``failed`` with the lake's ``lease_timeout`` reason. Nothing is
missing and nothing is asked of the vendor twice; the other request is fetching it, and it is stored when that one
finishes. A caller shows it as "being fetched by another request; ask again" and reads the chain again.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from app.data_lake import catalog_client, option_lake
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
from app.options_backtest.past_chain import MAX_WINDOW_CONTRACTS, ChainRefusal, unreadable_problem
from app.utils.background_loop import CallerStoppedWaitingError, background_loop

logger = logging.getLogger(__name__)

# Contracts one call takes: as many as a window of the chain can hold, so the contracts a window names are one download.
MAX_CONTRACTS_PER_DOWNLOAD = MAX_WINDOW_CONTRACTS

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
    underlying: str, trading_date: date, now_ms: int | None
) -> tuple[DownloadItem, OptionListing | None]:
    """The listing, captured or reused, and read once so each contract capture can be handed it."""
    async with option_lake.vendor_request_gate():
        result = await ensure_option_listing(underlying, trading_date, now_ms=now_ms)
    item = _item_of(ItemKind.LISTING, underlying, result)
    if item.outcome is Outcome.FAILED:
        return item, None
    what = f"the {underlying} listing for {trading_date.isoformat()}"
    try:
        stored = await asyncio.to_thread(read_option_listing, underlying, trading_date)
        detail = f"{what} was reported stored, and the lake no longer holds it"
    except (LakeAdmissionError, OptionListingFileError) as exc:
        stored = None
        detail = unreadable_problem(what, exc)
    if stored is None:
        return DownloadItem(ItemKind.LISTING, underlying, Outcome.FAILED, "listing_not_captured", detail), None
    return item, stored


async def _capture_underlying(underlying: str, trading_date: date, now_ms: int | None) -> DownloadItem:
    """The underlying's raw minute bars: left alone when held, refused when the session is not over, else fetched.

    A fetch is the stock engine's own function. It has no completed-session gate, so the lake's rule for the options of
    the same session is asked first, and a refusal is the item's reason and words as they are, as for a listing.
    ``now_ms`` is the clock for that rule; ``None`` is the lake's clock.
    """
    held_by = LakeRunData(underlying)
    try:
        held = await asyncio.to_thread(held_by.underlying_day, trading_date)
    except (LakeAdmissionError, UnderlyingDayUnreadable) as exc:
        # Repairing a damaged stock file is the stock side's job; nothing here knows the pipeline would heal it.
        detail = unreadable_problem(f"the {underlying} minute bars for {trading_date.isoformat()}", exc)
        return DownloadItem(ItemKind.UNDERLYING, underlying, Outcome.FAILED, "underlying_unreadable", detail)
    if held is not None:
        return DownloadItem(ItemKind.UNDERLYING, underlying, Outcome.ALREADY_STORED, None, None)
    refusal = option_lake.session_refusal(
        underlying, trading_date, now_ms=option_lake.current_ms() if now_ms is None else now_ms
    )
    if refusal is not None:
        return DownloadItem(ItemKind.UNDERLYING, underlying, Outcome.FAILED, refusal.reason, refusal.detail)
    async with option_lake.vendor_request_gate():
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
    now_ms: int | None,
) -> DownloadItem:
    async with option_lake.vendor_request_gate():
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

    ``now_ms`` is the clock of the lake's rules for all three captures (a session is taken only once its New York date
    has passed; the plan's window); ``None`` is the lake's own clock. A session that is not over, or not a session,
    fails the listing and the underlying with the lake's reason (and so each contract as ``listing_not_captured``),
    and nothing is asked of the vendor or of the stock engine.

    A contract another call is fetching at the same moment comes back ``failed`` with the lake's ``lease_timeout``
    reason: it is not missing, the other request is fetching it (see the module docstring); ask again.

    Cancelling the call (a client that hangs up) does not cancel a capture. The captures run on the background loop
    and the cancellation never reaches it: this waits for them through every further cancellation, and raises the
    cancellation only once each capture it began has ended, so none leaves a row it claimed ``fetching``. A capture
    that was waiting for a place at the gate is one it began, and ends too. What had not begun when the call was
    cancelled never does: the contracts are not started if the cancellation came during the listing and the
    underlying. Nothing is returned for a cancelled call, and an error the captures met after the cancellation is
    logged and dropped.
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
    # The vendor limit is a semaphore of one event loop, and the loop a run's download asks on is the process's
    # background loop; the server's own loop would count a second four. So the captures run there, and this waits.
    caller_has_gone = threading.Event()
    work = asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(
            _capture(underlying, trading_date, named, now_ms, caller_has_gone), background_loop()
        )
    )
    return await _waited_for(work, caller_has_gone)


async def _waited_for(
    work: asyncio.Future[tuple[DownloadItem, ...]], caller_has_gone: threading.Event
) -> tuple[DownloadItem, ...]:
    """The captures' result; if this task is cancelled meanwhile, wait for them to end and then raise the cancellation.

    A task that awaits ``work`` itself and is cancelled cancels it, and that cancels the coroutine on the other loop:
    a capture could stop after it claimed its row and leave it ``fetching`` until the lease runs out. ``asyncio.wait``
    never cancels what it waits for, so the cancellation stops at this task. It is swallowed, every time it comes (an
    ASGI server's cancel scope sends one at every checkpoint), until ``work`` is done; the captures are told the caller
    has gone, so they begin nothing more, and the first cancellation is raised when they have ended.
    """
    first_cancellation: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.wait([work])
        except asyncio.CancelledError as cancelled:
            first_cancellation = first_cancellation or cancelled
            caller_has_gone.set()
    if first_cancellation is None:
        return work.result()
    if not work.cancelled() and (error := work.exception()) is not None:
        logger.warning(
            "past-chain download: the captures met an error after the caller cancelled; it is dropped",
            extra={"action": "chain_download_error_after_cancel", "error_type": type(error).__name__},
            exc_info=error,
        )
    raise first_cancellation


async def _capture(
    underlying: str,
    trading_date: date,
    named: Sequence[OptionContract],
    now_ms: int | None,
    caller_has_gone: threading.Event,
) -> tuple[DownloadItem, ...]:
    """The listing and the underlying, then the contracts, as ``ensure_chain_data`` asks, run on the background loop.

    Nothing cancels this coroutine, so each stage is a plain gather that lets every capture finish before an error is
    raised. A caller that has gone (``caller_has_gone``) is the one thing that stops it, and only between stages: what
    has begun finishes. What it returns then is not read.
    """
    # The catalog's pool belongs to the loop that made it, and the lake's capture functions do not make one.
    await catalog_client.init_pool()
    if caller_has_gone.is_set():
        return ()
    # Every capture finishes before an error is raised: none is cancelled halfway through a claim.
    first = await asyncio.gather(
        _capture_listing(underlying, trading_date, now_ms),
        _capture_underlying(underlying, trading_date, now_ms),
        return_exceptions=True,
    )
    _raise_the_first_error(first)
    if caller_has_gone.is_set():
        return ()
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
        *(_capture_contract(contract, underlying, trading_date, listing, now_ms) for contract in named),
        return_exceptions=True,
    )
    _raise_the_first_error(contract_outcomes)
    return (listing_item, underlying_item, *(item for item in contract_outcomes if isinstance(item, DownloadItem)))
