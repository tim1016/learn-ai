"""One option contract's minute bars for one session, in the data lake (#3030).

A catalog row is the stock minute-trade key with the option market name and the
contract's own symbol (``OptionContract.occ_symbol``): ``time_series_bars``,
``minute``, ``trade``, ``polygon``, ``raw``. The file is the zip the lake writes
for a stock's minute trades (``lean_writer.build_minute_trade_zip_bytes``): a
bar's start as milliseconds after New York midnight, prices as integers of
ten-thousandths of a dollar. The vendor stamps a bar with its start and the
lake keeps that; turning a start into a close is the replay's one conversion.

``ensure_option_day_bars`` mirrors ``ensure_data._process_minute_trade_artifact``
step by step. Three things differ, all on purpose:

* A contract that was listed and never traded that session gets an empty answer
  from the vendor. That is a fact, not a failure: it is published as a complete
  artifact with zero rows, so it is pinned and never asked for again.
* A session before the plan's rolling window is ``outside_data_plan``, a failure
  of its own that a caller can tell from an outage and that is not retried to
  exhaustion.
* A session that is not over (``OPTION_SESSION_SETTLE_MS`` after its close) is
  refused before any claim.

``read_option_day_bars`` reads only bytes the catalog admits.
"""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import read_committed_bytes
from app.data_lake.bar_validation import CorruptVendorBarsError, assert_publishable_minute_bars
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.ensure_data import _polygon_bar_to_minute_trade_bar
from app.data_lake.lean_writer import build_minute_trade_zip_bytes, parse_volume
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
    publish_option_artifact,
    refusal_before_claim,
    stored_file_still_reads,
    vendor_reason,
)
from app.data_lake.path_policy import OptionMinuteBarPath, resolve_lake_root
from app.data_lake.polygon_fetcher import PolygonFetchError, fetch_minute_trade_aggregates
from app.data_lake.types import ArtifactIdentity, ArtifactRecord
from app.options.contract import OptionContract
from app.utils.timestamps import to_ms_utc

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_DECI_CENTS_PER_DOLLAR = Decimal(10_000)


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


def decode_deci_cent(ticks: int) -> Decimal:
    """A stored price, as the exact decimal it names.

    Formula: price = ticks / 10 000, where ticks is an integer count of ten-thousandths of a dollar
    Reference: LEAN's scale factor 1/10 000 (``TradeBar._scaleFactor`` in Common/Data/Market/TradeBar.cs),
      as written by ``lean_writer.to_deci_cent``
    Canonical implementation: this file, for option bars; the stock reader divides by the same scale
      inline in ``app/engine/data/lean_format._parse_csv_bytes``
    Validated against: tests/unit/data_lake/test_option_bars.py::test_a_premium_of_1_27_reads_back_as_the_exact_decimal_1_27
      and ::test_every_price_on_the_ten_thousandth_grid_survives_the_round_trip_exactly

    Decimal division of an integer by 10 000 is exact (a quotient with at most four decimal places), so
    no price passes through a float on the way out.
    """
    return Decimal(ticks) / _DECI_CENTS_PER_DOLLAR


def parse_option_day_zip(payload: bytes, contract_symbol: str, trading_date: date) -> tuple[OptionBar, ...]:
    """The bars in one stored session file; strict, where the stock reader deliberately skips a bad row.

    The file must hold exactly the one CSV member this module writes, named for this
    contract and date; every row must have six well-formed columns. A file that is
    anything else is :class:`OptionBarsFileError`, never a shorter list of bars.
    """
    expected = f"{trading_date:%Y%m%d}_{contract_symbol.lower()}_minute_trade.csv"
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        if zf.namelist() != [expected]:
            raise OptionBarsFileError(f"expected one member {expected!r}, found {zf.namelist()!r}")
        text = zf.read(expected).decode("ascii")
    midnight = datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=_ET)
    bars: list[OptionBar] = []
    for number, line in enumerate(text.splitlines(), start=1):
        columns = line.split(",")
        if len(columns) != 6:
            raise OptionBarsFileError(f"{expected} line {number}: expected 6 columns, found {len(columns)}")
        start_text, *price_texts, volume_text = columns
        try:
            start_ms = to_ms_utc(midnight + timedelta(milliseconds=int(start_text)))
            open_, high, low, close = (decode_deci_cent(int(t)) for t in price_texts)
            volume = parse_volume(volume_text)
        except ValueError as exc:
            raise OptionBarsFileError(f"{expected} line {number}: {exc}") from exc
        bars.append(OptionBar(start_ms=start_ms, open=open_, high=high, low=low, close=close, volume=volume))
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


async def ensure_option_day_bars(
    contract: OptionContract, trading_date: date, *, now_ms: int | None = None
) -> OptionCaptureResult:
    """Capture one contract's session into the lake, or reuse what is there.

    Claim, reuse-or-refresh on a lost claim, reclaim, fetch, validate, encode,
    publish under the lease; a typed failure on every vendor error. ``now_ms`` fixes
    the clock for the "is the session over" rule and the plan's window; it defaults
    to the wall clock.
    """
    symbol = contract.occ_symbol
    now = now_ms if now_ms is not None else current_ms()
    refusal = refusal_before_claim(symbol, trading_date, now_ms=now)
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
        polygon_bars = await fetch_minute_trade_aggregates(
            symbol=f"O:{symbol}",
            start=trading_date,
            end=trading_date,
            api_key=settings.POLYGON_API_KEY,
            adjusted=False,
        )
    except PolygonFetchError as e:
        return await fail(vendor_reason(e, trading_date, now_ms=now), str(e))

    # No bars is not a failure here: the contract was listed and did not trade, and
    # that is pinned as a complete artifact with no rows (the stock path fails it).
    try:
        assert_publishable_minute_bars(polygon_bars, symbol=symbol, trading_date=trading_date)
    except CorruptVendorBarsError as e:
        return await fail("validation_failed", str(e))

    payload = build_minute_trade_zip_bytes(
        symbol=symbol,
        trading_date_yyyymmdd=trading_date.strftime("%Y%m%d"),
        bars=[_polygon_bar_to_minute_trade_bar(b) for b in polygon_bars],
    )
    # The publication functions take plain ints for the first and last bar start.
    # A session with no bars records 0 and 0, as the metadata rows do; the row count says there are none.
    first_bar_ms = polygon_bars[0].t_ms if polygon_bars else 0
    last_bar_ms = polygon_bars[-1].t_ms if polygon_bars else 0
    published = await publish_option_artifact(
        payload=payload,
        rel_path=rel_path,
        lease=held,
        row_count=len(polygon_bars),
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
        extra={"action": "option_day_captured", "symbol": symbol, "trading_date": trading_date.isoformat(), "rows": len(polygon_bars)},
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
            row_count=len(polygon_bars),
            first_bar_start_ms=first_bar_ms,
            last_bar_start_ms=last_bar_ms,
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        failure=None,
        reused=False,
    )

