"""One option contract's minute bars for one session, in the data lake (#3030).

A catalog row is the stock minute-trade key with the option market name and the
contract's own symbol (``OptionContract.occ_symbol``): ``time_series_bars``,
``minute``, ``trade``, ``polygon``, ``raw``. The file is the zip the lake writes
for a stock's minute trades (``lean_writer.build_minute_trade_zip_bytes``): a
bar's start as milliseconds after New York midnight, prices as integers of
ten-thousandths of a dollar. The vendor stamps a bar with its start and the
lake keeps that; turning a start into a close is the replay's one conversion.

``ensure_option_day_bars`` mirrors ``ensure_data._process_minute_trade_artifact``
step by step. Four things differ, all on purpose:

* A contract that was listed and never traded that session gets an empty answer
  from the vendor. That is a fact, not a failure: it is published as a complete
  artifact with zero rows, so it is pinned and never asked for again. But the vendor
  gives the same empty answer for a contract that never existed, one that expired,
  and a day with no session, so an empty answer proves nothing by itself. The day is
  captured only for a contract the stored listing of the same date holds
  (``listed_under`` names whose listing), and only from a well-formed answer
  (``polygon_option_bars``).
* A session before the plan's rolling window is ``outside_data_plan``, a failure
  of its own that a caller can tell from an outage; it is refused before any claim,
  and a day at the edge of the window is told so by the vendor's 403.
* A session that is not over (``OPTION_SESSION_SETTLE_MS`` after its close) is
  refused before any claim.
* The vendor's digits go to the stored integer as decimals, never through a float.

``read_option_day_bars`` reads only bytes the catalog admits.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import lzma
import re
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.bar_validation import (
    CorruptVendorBarsError,
    assert_publishable_minute_bars,
    assert_publishable_stored_minute_bars,
)
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.lean_writer import (
    MinuteTradeBar,
    build_minute_trade_zip_bytes,
    from_deci_cent,
    parse_volume,
    to_deci_cent,
)
from app.data_lake.option_lake import (
    LEASE_TTL_MS,
    OPTION_MARKET,
    OPTION_PRICE_ADJUSTMENT_MODE,
    OPTION_PROVIDER,
    WORKER_ID,
    OptionCaptureResult,
    OptionDataFailure,
    OptionFailureReason,
    claim_or_take_over,
    current_ms,
    fail_row,
    plan_window_refusal,
    publish_option_artifact,
    refusal_before_claim,
    stored_file_still_reads,
    vendor_reason,
)
from app.data_lake.option_listing import read_option_listing
from app.data_lake.path_policy import OptionMinuteBarPath, resolve_lake_root
from app.data_lake.polygon_fetcher import PolygonBar, PolygonFetchError
from app.data_lake.polygon_option_bars import PolygonOptionBar, fetch_option_minute_bars
from app.data_lake.polygon_option_http import OPTION_TICKER_PREFIX
from app.data_lake.types import ArtifactIdentity, ArtifactRecord, is_lake_addressable_symbol
from app.options.contract import OptionContract
from app.utils.timestamps import to_ms_utc

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
#: A bar's start and its four prices as the file writes them: digits and nothing else.
_UNSIGNED_INTEGER = re.compile(r"[0-9]+")


def option_minute_trade_dch() -> str:
    """The recipe an option contract's minute-trade file is made under.

    Not ``ensure_data._minute_trade_dch``: that one adds a fractional-volume key
    from 2026-02-23 on, for stock shares. An option's volume is whole contracts,
    and the recipe does not vary by date. The ticker the vendor is asked for is
    the contract's symbol behind ``O:``.
    """
    return _dch(
        provider=OPTION_PROVIDER,
        provider_params={
            "endpoint": "v2/aggs",
            "asset_class": "option",
            "ticker_prefix": "O:",
            "timespan": "minute",
            "multiplier": 1,
            "adjusted": False,
        },
        price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
        session_policy="full",
        lean_format_version=1,
    )


@dataclass(frozen=True)
class OptionBar:
    """One minute of trades in one contract. ``start_ms`` is the bar's start, epoch ms UTC."""

    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class OptionDayBars:
    """A captured session of one contract: its bars (none, if it never traded) and the file's sha256 to pin."""

    contract_symbol: str
    trading_date: date
    bars: tuple[OptionBar, ...]
    sha256: str


class OptionBarsFileError(ValueError):
    """A stored option-bars file is not the file this module writes."""


def _member_text(payload: bytes, member: str) -> str:
    """The text of the one CSV member a bars file holds; anything else is :class:`OptionBarsFileError`."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            raw = zf.read(member) if names == [member] else None
    except (
        zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, NotImplementedError, RuntimeError, OSError, ValueError,
        ArithmeticError,
    ) as exc:
        # Not a zip, one with damaged offsets or data, a truncated stream, an unsupported or encrypted member.
        raise OptionBarsFileError(f"{member}: not a readable bars file ({type(exc).__name__}: {exc})") from exc
    if raw is None:
        raise OptionBarsFileError(f"expected one member {member!r}, found {names!r}")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OptionBarsFileError(f"{member}: the rows are not plain text ({exc})") from exc


def _stored_bar(bar: OptionBar) -> MinuteTradeBar:
    return MinuteTradeBar(
        bar_start_et=_bar_start_et(bar.start_ms), open=bar.open, high=bar.high, low=bar.low, close=bar.close,
        volume=bar.volume,
    )


def parse_option_day_zip(payload: bytes, contract_symbol: str, trading_date: date) -> tuple[OptionBar, ...]:
    """The bars in one stored session file; strict, where the stock reader deliberately skips a bad row.

    The file must hold exactly the one CSV member this module writes, named for this
    contract and date, of plain text with six columns in every row: the start and the
    four prices as digits, then the volume. Its bars must also obey the rules a stored
    day obeys (``bar_validation.assert_publishable_stored_minute_bars``): every price
    above zero, the low and the high bracketing the open and the close, starts strictly
    increasing and inside the date. A file that is anything else, a zip that is not
    one, bytes that are not text and a blank line included, is :class:`OptionBarsFileError`
    with its cause chained, never a shorter list of bars and never a raw ``zipfile`` or
    decoding error.
    """
    expected = f"{trading_date:%Y%m%d}_{contract_symbol.lower()}_minute_trade.csv"
    text = _member_text(payload, expected)
    midnight = datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=_ET)
    bars: list[OptionBar] = []
    for number, line in enumerate(text.splitlines(), start=1):
        columns = line.split(",")
        if len(columns) != 6:
            raise OptionBarsFileError(f"{expected} line {number}: expected 6 columns, found {len(columns)}")
        start_text, *price_texts, volume_text = columns
        if any(_UNSIGNED_INTEGER.fullmatch(t) is None for t in (start_text, *price_texts)):
            raise OptionBarsFileError(f"{expected} line {number}: the start and the four prices must be whole numbers")
        try:
            start_ms = to_ms_utc(midnight + timedelta(milliseconds=int(start_text)))
            open_, high, low, close = (from_deci_cent(int(t)) for t in price_texts)
            volume = parse_volume(volume_text)
        except (ValueError, ArithmeticError) as exc:  # a start past the calendar's end is an OverflowError
            raise OptionBarsFileError(f"{expected} line {number}: {exc}") from exc
        bars.append(OptionBar(start_ms=start_ms, open=open_, high=high, low=low, close=close, volume=volume))
    try:
        assert_publishable_stored_minute_bars(
            [_stored_bar(bar) for bar in bars], symbol=contract_symbol, trading_date=trading_date
        )
    except CorruptVendorBarsError as exc:
        raise OptionBarsFileError(f"{expected}: the stored bars break the rules of a stored day: {exc}") from exc
    return tuple(bars)


def _bars_path(contract: OptionContract, trading_date: date) -> PurePosixPath:
    return OptionMinuteBarPath(
        underlying=contract.root, contract_symbol=contract.occ_symbol, trading_date=trading_date
    ).relative_path()


def read_option_day_bars(
    contract: OptionContract,
    trading_date: date,
    *,
    admission: Callable[[Path], bytes] = read_committed_bytes,
) -> OptionDayBars | None:
    """One contract's captured session, or ``None`` when that day was never captured.

    A captured day with no trades is an :class:`OptionDayBars` with no bars, which
    is not the same as ``None``. Prices are exact ``Decimal`` and a bar's start is
    epoch ms UTC. The bytes are read only through ``admission`` (the catalog's
    committed receipt for exactly these bytes by default; a research worker that
    owns no catalog connection passes one that asks its parent), so a file that is
    not admitted raises ``LakeAdmissionError`` rather than reading. Synchronous, like
    ``LeanMinuteDataReader.read_day``: call it from a worker thread.
    """
    symbol = contract.occ_symbol
    path = resolve_lake_root(OPTION_PRICE_ADJUSTMENT_MODE) / Path(*_bars_path(contract, trading_date).parts)
    if not path.is_file():
        return None
    payload = admission(path)
    return OptionDayBars(
        contract_symbol=symbol,
        trading_date=trading_date,
        bars=parse_option_day_zip(payload, symbol, trading_date),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


async def _listing_refusal(
    listed_under: str, symbol: str, trading_date: date
) -> OptionDataFailure | None:
    """Does the stored listing of ``listed_under`` for ``trading_date`` hold the contract? A refusal if not.

    Read through the catalog's admission, like any reader of the lake. A listing that is
    there but not admitted or not readable is no usable listing: it is refused as not
    captured, and capturing the listing again repairs it. An outage of the catalog
    is not a verdict on the listing and propagates.
    """
    try:
        listing = await asyncio.to_thread(read_option_listing, listed_under, trading_date)
    except (LakeAdmissionError, OSError, zipfile.BadZipFile, ValueError) as exc:
        return OptionDataFailure(
            "listing_not_captured",
            f"the stored listing of {listed_under} for {trading_date.isoformat()} is not admitted or does not read "
            f"({type(exc).__name__}); capture the listing again",
            symbol,
            trading_date,
        )
    if listing is None:
        return OptionDataFailure(
            "listing_not_captured",
            f"no listing of {listed_under} is stored for {trading_date.isoformat()}; capture it first",
            symbol,
            trading_date,
        )
    if symbol not in {row.ticker.removeprefix(OPTION_TICKER_PREFIX) for row in listing.rows}:
        return OptionDataFailure(
            "not_listed",
            f"{symbol} is not in the {listed_under} listing stored for {trading_date.isoformat()}",
            symbol,
            trading_date,
        )
    return None


def _bar_start_et(t_ms: int) -> datetime:
    return (datetime.fromtimestamp(0, tz=UTC) + timedelta(milliseconds=t_ms)).astimezone(_ET)


def _minute_trade_bar(bar: PolygonOptionBar) -> MinuteTradeBar:
    """The vendor's decimals as the lake's bar type, with no float on the way."""
    return MinuteTradeBar(
        bar_start_et=_bar_start_et(bar.t_ms),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def _publication_gate_view(bars: Sequence[PolygonOptionBar]) -> list[PolygonBar]:
    """The bars in the shape the lake's publication gate takes.

    The gate's checks (finite, positive, high above low, ordered, inside the date) read
    floats; they decide whether to publish and never what is stored. The file is encoded from the decimals.
    """
    return [
        PolygonBar(
            t_ms=b.t_ms, open=float(b.open), high=float(b.high), low=float(b.low), close=float(b.close),
            volume=b.volume, vwap=float(b.close), n=0,
        )
        for b in bars
    ]


def _require_prices_above_zero_on_the_grid(bars: Sequence[PolygonOptionBar]) -> None:
    """A positive price under half a ten-thousandth of a dollar would be stored as 0; the file never holds one.

    The publication gate judges the vendor's number. The file holds it rounded onto the
    ten-thousandth grid (``to_deci_cent``), and every reader is told every price is above zero.
    """
    for index, bar in enumerate(bars):
        for name in ("open", "high", "low", "close"):
            price = getattr(bar, name)
            if to_deci_cent(price) == 0:
                raise ValueError(
                    f"bar[{index}] t_ms={bar.t_ms}: {name}={price} is under half a ten-thousandth of a dollar "
                    "and would be stored as 0"
                )


async def ensure_option_day_bars(
    contract: OptionContract, trading_date: date, *, listed_under: str, now_ms: int | None = None
) -> OptionCaptureResult:
    """Capture one contract's session into the lake, or reuse what is there.

    ``listed_under`` is the underlying whose listing holds the contract (a contract's
    OCC root is not always its underlying's ticker: ``BRK.B`` options are ``BRKB...``).
    Before any claim and any vendor call it refuses, as a typed failure and with no row:
    a symbol the catalog cannot hold (``symbol_too_long``), a day with no session
    (``not_a_session``), a session not yet over (``session_not_over``), a session older
    than the plan reaches (``outside_data_plan``), a date whose listing of that underlying
    is not stored (``listing_not_captured``), and a contract that listing does not hold
    (``not_listed``). Only then does it claim, reuse-or-refresh on a lost claim, reclaim,
    fetch, validate, encode and publish under the lease. Every vendor outcome, a malformed
    answer included, is a typed failure on the row and none raises. ``now_ms`` fixes the
    clock for the session and plan rules; it defaults to the wall clock.
    """
    if not is_lake_addressable_symbol(listed_under):
        raise ValueError(f"listed_under must be an upper-case ticker, got {listed_under!r}")
    symbol = contract.occ_symbol
    now = now_ms if now_ms is not None else current_ms()
    refusal = refusal_before_claim(symbol, trading_date, now_ms=now) or plan_window_refusal(
        symbol, trading_date, now_ms=now
    )
    if refusal is None:
        refusal = await _listing_refusal(listed_under, symbol, trading_date)
    if refusal is not None:
        return OptionCaptureResult.failed(refusal)

    rel_path = _bars_path(contract, trading_date)
    dch = option_minute_trade_dch()
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market=OPTION_MARKET,
        symbol=symbol,
        trading_date=trading_date,
        resolution="minute",
        data_type="trade",
        provider=OPTION_PROVIDER,
        price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
    )

    async def claim() -> int | None:
        return await catalog_client.claim_minute_bar(
            identity=identity,
            worker_id=WORKER_ID,
            lease_ttl_ms=LEASE_TTL_MS,
            data_contract_hash=dch,
            file_path=str(rel_path),
        )

    async def select_complete() -> ArtifactRecord | None:
        existing = await catalog_client.select_coverage_minute_bars(
            market=OPTION_MARKET,
            symbol=symbol,
            data_type="trade",
            start_trading_date=trading_date,
            end_trading_date=trading_date,
            price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
        )
        return existing[0] if existing else None

    async def stored_file_reads(record: ArtifactRecord) -> bool:
        return await stored_file_still_reads(record, rel_path, lambda payload: parse_option_day_zip(payload, symbol, trading_date))

    held = await claim_or_take_over(
        claim=claim,
        select_complete=select_complete,
        read_claim_state=lambda: catalog_client.select_minute_bar_claim_state(identity),
        stored_file_reads=stored_file_reads,
        data_contract_hash=dch,
        symbol=symbol,
        trading_date=trading_date,
    )
    if isinstance(held, OptionCaptureResult):
        return held

    async def fail(reason: OptionFailureReason, detail: str) -> OptionCaptureResult:
        return await fail_row(
            artifact_id=held.artifact_id,
            lease_generation=held.lease_generation,
            reason=reason,
            detail=detail,
            symbol=symbol,
            trading_date=trading_date,
        )

    try:
        bars = await fetch_option_minute_bars(symbol, trading_date, settings.POLYGON_API_KEY)
    except PolygonFetchError as e:
        return await fail(vendor_reason(e), str(e))

    # No bars is a fact here, not a failure: the vendor confirmed an empty answer for a
    # contract the stored listing of this date holds, so it was listed and did not trade.
    # That is pinned as a complete artifact with no rows (the stock path fails it).
    try:
        assert_publishable_minute_bars(_publication_gate_view(bars), symbol=symbol, trading_date=trading_date)
    except CorruptVendorBarsError as e:
        return await fail("validation_failed", str(e))

    try:
        _require_prices_above_zero_on_the_grid(bars)
        payload = build_minute_trade_zip_bytes(
            symbol=symbol,
            trading_date_yyyymmdd=trading_date.strftime("%Y%m%d"),
            bars=[_minute_trade_bar(b) for b in bars],
        )
    except (ArithmeticError, ValueError) as e:
        # A price or volume the file cannot hold (the gate passes any finite positive number).
        return await fail("validation_failed", f"the bars of {symbol} {trading_date.isoformat()} cannot be written: {e}")
    # The publication functions take plain ints for the first and last bar start.
    # A session with no bars records 0 and 0, as the metadata rows do; the row count says there are none.
    first_bar_ms = bars[0].t_ms if bars else 0
    last_bar_ms = bars[-1].t_ms if bars else 0
    published = await publish_option_artifact(
        payload=payload,
        rel_path=rel_path,
        lease=held,
        row_count=len(bars),
        first_bar_start_ms=first_bar_ms,
        last_bar_start_ms=last_bar_ms,
        data_contract_hash=dch,
        symbol=symbol,
        trading_date=trading_date,
    )
    if isinstance(published, OptionDataFailure):
        return OptionCaptureResult.failed(published)

    logger.info(
        "data_lake.option_bars: session captured",
        extra={"action": "option_day_captured", "symbol": symbol, "trading_date": trading_date.isoformat(), "rows": len(bars)},
    )
    return OptionCaptureResult(
        record=ArtifactRecord(
            id=held.artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            resolution=identity.resolution,
            data_type=identity.data_type,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=dch,
            file_path=str(rel_path),
            file_sha256=published,
            row_count=len(bars),
            first_bar_start_ms=first_bar_ms,
            last_bar_start_ms=last_bar_ms,
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        failure=None,
        reused=False,
    )
