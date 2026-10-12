"""The option contracts listed for an underlying as of a date, in the data lake (#3030).

A listing is a ``metadata`` catalog row with the option market name and the
underlying as its symbol; the row's ``DataContractHash`` is its identity (the
provider, the endpoint, the underlying and the as-of date), because a metadata
row has no queryable date. Unlike the LEAN metadata bundle, which publishes
first and records after, a listing is claimed first and published under its
lease (``atomic.publish_artifact``), so a writer that lost its lease cannot
overwrite the winner.

The file is a zip with one CSV member, a header row and one row per contract,
sorted by the vendor's ticker so the same listing always makes the same bytes:
the ticker, call or put, the strike in thousandths of a dollar, the expiry as
``YYYYMMDD``, the exercise style, the shares per contract, and whether the
vendor listed additional underlyings. Whether a contract is *standard* is not
stored: it is a rule applied when the file is read, so the rule can change
without a refetch.

A listing is admitted only if it is whole: the fetcher either returns every
page or raises, and an empty answer is refused, not pinned.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import read_committed_bytes
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.lean_writer import _DETERMINISTIC_ZIP_DATE_TIME
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
from app.data_lake.path_policy import OptionListingPath, resolve_lake_root
from app.data_lake.polygon_fetcher import PolygonFetchError
from app.data_lake.polygon_option_contracts import PolygonOptionContract, fetch_option_contracts
from app.data_lake.types import ArtifactIdentity, ArtifactRecord, is_lake_addressable_symbol
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options.contract import OptionContract, strike_thousandths
from app.options.expiry import option_expiry_anchor_ms

logger = logging.getLogger(__name__)

OPTION_LISTING_ENDPOINT = "v3/reference/options/contracts"
#: Bump when the file's columns or encoding change, so old rows are rebuilt under the new recipe.
OPTION_LISTING_FILE_FORMAT = 1
_HEADER = (
    "ticker",
    "contract_type",
    "strike_thousandths",
    "expiry",
    "exercise_style",
    "shares_per_contract",
    "additional_underlyings",
)
_STANDARD_EXERCISE_STYLE = "american"
_STANDARD_SHARES_PER_CONTRACT = 100
_VENDOR_TICKER_PREFIX = "O:"


def option_listing_dch(underlying: str, as_of: date) -> str:
    """A listing's identity: the provider, the endpoint, the underlying, and the as-of date's session open."""
    return _dch(
        provider=OPTION_PROVIDER,
        provider_params={
            "endpoint": OPTION_LISTING_ENDPOINT,
            "underlying": underlying,
            "as_of_ms": session_open_ms_utc(as_of),
            "file_format": OPTION_LISTING_FILE_FORMAT,
        },
        price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
        session_policy="full",
        lean_format_version=1,
    )


@dataclass(frozen=True)
class OptionListingRow:
    """One listed contract, as the file holds it, plus whether it is standard.

    ``expiry_ms`` is the expiry date's 16:00 New York anchor (ADR 0022 (i)).
    ``contract`` is the :class:`OptionContract` the ticker names, set only when the
    ticker parses and agrees with the row's own type, strike and expiry; ``standard``
    is the rule in :func:`_classify`, and a standard row always has a contract.
    """

    ticker: str
    contract_type: str
    strike_thousandths: int
    expiry_ms: int
    exercise_style: str | None
    shares_per_contract: int | None
    has_additional_underlyings: bool
    standard: bool
    contract: OptionContract | None


@dataclass(frozen=True)
class OptionListing:
    """The contracts listed for an underlying as of a date, and the file's sha256 to pin."""

    underlying: str
    as_of: date
    rows: tuple[OptionListingRow, ...]
    sha256: str


class OptionListingFileError(ValueError):
    """A vendor contract cannot be held by the file, or a stored file is not the file this module writes."""


def _member_name(underlying: str, as_of: date) -> str:
    return f"{as_of:%Y%m%d}_{underlying.lower()}_contracts.csv"


def _occ_root(underlying: str) -> str:
    """The root an OCC symbol writes for a stock: its letters (``BRK.B`` is ``BRKB``)."""
    return underlying.replace(".", "")


def _csv_line(contract: PolygonOptionContract) -> list[str]:
    ticker = contract.ticker
    try:
        strike = strike_thousandths(contract.strike)
    except ValueError as exc:
        raise OptionListingFileError(f"{ticker}: strike_price is not in whole thousandths of a dollar: {exc}") from exc
    try:
        expiry = date.fromisoformat(contract.expiration_date)
    except ValueError as exc:
        raise OptionListingFileError(f"{ticker}: expiration_date {contract.expiration_date!r} is not a date") from exc
    shares = ""
    if contract.shares_per_contract is not None:
        if contract.shares_per_contract != contract.shares_per_contract.to_integral_value():
            raise OptionListingFileError(
                f"{ticker}: shares_per_contract {contract.shares_per_contract} is not a whole number"
            )
        shares = str(int(contract.shares_per_contract))
    return [
        ticker,
        contract.contract_type.lower(),
        str(strike),
        f"{expiry:%Y%m%d}",
        (contract.exercise_style or "").lower(),
        shares,
        "1" if contract.has_additional_underlyings else "0",
    ]


def build_option_listing_zip_bytes(underlying: str, as_of: date, contracts: Sequence[PolygonOptionContract]) -> bytes:
    """The listing's file. Deterministic: the same contracts, in any order, make the same bytes.

    Nothing is repaired: a duplicate ticker, a strike finer than a thousandth, an
    expiry that is not a date or a share count that is not whole raises
    :class:`OptionListingFileError` naming the ticker, and no file is made.
    """
    lines = sorted((_csv_line(c) for c in contracts), key=lambda line: line[0])
    for earlier, later in pairwise(lines):
        if earlier[0] == later[0]:
            raise OptionListingFileError(f"duplicate ticker {earlier[0]} in the listing")
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(_HEADER)
    writer.writerows(lines)

    buf = io.BytesIO()
    info = zipfile.ZipInfo(filename=_member_name(underlying, as_of), date_time=_DETERMINISTIC_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, text.getvalue())
    return buf.getvalue()


def _classify(
    *,
    underlying: str,
    ticker: str,
    contract_type: str,
    strike: int,
    expiry_ms: int,
    exercise_style: str | None,
    shares: int | None,
    additional: bool,
) -> tuple[bool, OptionContract | None]:
    """Is the row standard, and which contract does its ticker name?

    Standard: American, 100 shares, no additional underlyings, and a ticker that
    parses to a contract of this underlying and agrees with the row's own type,
    strike and expiry. A ticker this system cannot name (an adjusted root carries a
    digit) or one that names another contract than its row keeps the row, unnamed.
    """
    try:
        named = OptionContract.from_occ_symbol(ticker.removeprefix(_VENDOR_TICKER_PREFIX))
    except ValueError:
        return False, None
    if named.right != contract_type or named.strike_thousandths != strike or named.expiry_ms != expiry_ms:
        return False, None
    standard = (
        named.root == _occ_root(underlying)
        and exercise_style == _STANDARD_EXERCISE_STYLE
        and shares == _STANDARD_SHARES_PER_CONTRACT
        and not additional
    )
    return standard, named


def parse_option_listing_zip(payload: bytes, underlying: str, as_of: date) -> tuple[OptionListingRow, ...]:
    """The rows in one stored listing; strict, and standard-ness applied here, on read."""
    expected = _member_name(underlying, as_of)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        if zf.namelist() != [expected]:
            raise OptionListingFileError(f"expected one member {expected!r}, found {zf.namelist()!r}")
        text = zf.read(expected).decode("utf-8")
    lines = csv.reader(io.StringIO(text))
    header = next(lines, None)
    if header != list(_HEADER):
        raise OptionListingFileError(f"{expected}: unexpected header {header!r}")
    rows: list[OptionListingRow] = []
    for number, fields in enumerate(lines, start=2):
        if len(fields) != len(_HEADER):
            raise OptionListingFileError(f"{expected} line {number}: expected {len(_HEADER)} columns, found {len(fields)}")
        ticker, contract_type, strike_text, expiry_text, style_text, shares_text, additional_text = fields
        try:
            strike = int(strike_text)
            expiry_ms = option_expiry_anchor_ms(datetime.strptime(expiry_text, "%Y%m%d").date())
            shares = int(shares_text) if shares_text else None
            additional = {"0": False, "1": True}[additional_text]
        except (ValueError, KeyError) as exc:
            raise OptionListingFileError(f"{expected} line {number}: {exc!r}") from exc
        style = style_text or None
        standard, named = _classify(
            underlying=underlying, ticker=ticker, contract_type=contract_type, strike=strike, expiry_ms=expiry_ms,
            exercise_style=style, shares=shares, additional=additional,
        )
        rows.append(
            OptionListingRow(
                ticker=ticker,
                contract_type=contract_type,
                strike_thousandths=strike,
                expiry_ms=expiry_ms,
                exercise_style=style,
                shares_per_contract=shares,
                has_additional_underlyings=additional,
                standard=standard,
                contract=named,
            )
        )
    return tuple(rows)


def read_option_listing(
    underlying: str,
    as_of: date,
    *,
    admission: Callable[[Path], bytes] = read_committed_bytes,
) -> OptionListing | None:
    """The listing captured for an underlying as of a date, or ``None`` when that date was never captured.

    Read only through ``admission`` (the catalog's committed receipt for exactly these
    bytes by default), so a file the catalog does not admit raises ``LakeAdmissionError``
    rather than reading. Synchronous: call it from a worker thread.
    """
    relative = OptionListingPath(underlying=underlying, as_of=as_of).relative_path()
    path = resolve_lake_root(OPTION_PRICE_ADJUSTMENT_MODE) / Path(*relative.parts)
    if not path.is_file():
        return None
    payload = admission(path)
    return OptionListing(
        underlying=underlying,
        as_of=as_of,
        rows=parse_option_listing_zip(payload, underlying, as_of),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


async def ensure_option_listing(
    underlying: str, as_of: date, *, now_ms: int | None = None
) -> OptionCaptureResult:
    """Capture the contracts listed for ``underlying`` as of ``as_of`` into the lake, or reuse what is there.

    Claim, reuse-or-refresh on a lost claim, reclaim, fetch every page, build the
    file, publish under the lease; a typed failure on every vendor error. The same
    refusals as a session's bars come first (an unstorable symbol, a day with no
    session, a session not over), and cost no claim.
    """
    if not is_lake_addressable_symbol(underlying):
        raise ValueError(f"underlying must be an upper-case ticker, got {underlying!r}")
    now = now_ms if now_ms is not None else current_ms()
    refusal = refusal_before_claim(underlying, as_of, now_ms=now)
    if refusal is not None:
        return OptionCaptureResult.failed(refusal)

    rel_path = OptionListingPath(underlying=underlying, as_of=as_of).relative_path()
    dch = option_listing_dch(underlying, as_of)
    identity = ArtifactIdentity(
        artifact_kind="metadata",
        market=OPTION_MARKET,
        symbol=underlying,
        provider=OPTION_PROVIDER,
        price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
    )

    async def claim() -> int | None:
        return await catalog_client.claim_metadata_artifact(
            identity=identity,
            worker_id=WORKER_ID,
            lease_ttl_ms=LEASE_TTL_MS,
            data_contract_hash=dch,
            file_path=str(rel_path),
        )

    async def stored_file_reads(record: ArtifactRecord) -> bool:
        return await stored_file_still_reads(
            record, rel_path, lambda payload: parse_option_listing_zip(payload, underlying, as_of)
        )

    held = await claim_or_take_over(
        claim=claim,
        select_complete=lambda: catalog_client.select_complete_metadata_artifact(dch),
        read_claim_state=lambda: catalog_client.select_metadata_claim_state(dch),
        stored_file_reads=stored_file_reads,
        data_contract_hash=dch,
        symbol=underlying,
        trading_date=as_of,
    )
    if isinstance(held, OptionCaptureResult):
        return held

    async def fail(reason: OptionFailureReason, detail: str) -> OptionCaptureResult:
        return await fail_row(
            artifact_id=held.artifact_id,
            lease_generation=held.lease_generation,
            reason=reason,
            detail=detail,
            symbol=underlying,
            trading_date=as_of,
        )

    try:
        contracts = await fetch_option_contracts(underlying, as_of, settings.POLYGON_API_KEY)
    except PolygonFetchError as e:
        return await fail(vendor_reason(e, as_of, now_ms=now, plan_window=False), str(e))

    # An empty listing cannot be told from a vendor that answered nothing, and a
    # pinned empty chain would hide every contract of that date from then on.
    if not contracts:
        return await fail("provider_no_data", f"Polygon listed no contracts for {underlying} as of {as_of.isoformat()}")
    try:
        payload = build_option_listing_zip_bytes(underlying, as_of, contracts)
    except OptionListingFileError as e:
        return await fail("validation_failed", str(e))

    published = await publish_option_artifact(
        payload=payload,
        rel_path=rel_path,
        lease=held,
        row_count=len(contracts),
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        data_contract_hash=dch,
        symbol=underlying,
        trading_date=as_of,
    )
    if isinstance(published, OptionDataFailure):
        return OptionCaptureResult.failed(published)

    logger.info(
        "data_lake.option_listing: listing captured",
        extra={"action": "option_listing_captured", "underlying": underlying, "as_of": as_of.isoformat(), "contracts": len(contracts)},
    )
    return OptionCaptureResult(
        record=ArtifactRecord(
            id=held.artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=None,
            resolution=None,
            data_type=None,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=dch,
            file_path=str(rel_path),
            file_sha256=published,
            row_count=len(contracts),
            first_bar_start_ms=0,
            last_bar_start_ms=0,
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        failure=None,
        reused=False,
    )


async def captured_listing_dates(underlying: str, start: date, end: date) -> tuple[date, ...]:
    """The as-of dates in ``[start, end]`` whose listing for ``underlying`` is captured, from one catalog query.

    A listing row has no queryable date, so the underlying's complete listing rows
    are read at once and each session of the range is kept if the hash of its
    listing is among them. Dates that are not sessions are never listings.
    """
    held = {
        record.data_contract_hash
        for record in await catalog_client.select_complete_metadata_artifacts(
            OPTION_MARKET, underlying, OPTION_PROVIDER, OPTION_PRICE_ADJUSTMENT_MODE
        )
    }
    return tuple(day for day in expected_sessions(start, end) if option_listing_dch(underlying, day) in held)
