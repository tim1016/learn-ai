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

Every row the vendor sends with a ticker is kept. A field the vendor left out or
wrote as something the file cannot hold (a strike that is not a finite number
above zero or finer than a thousandth, a share count that is not a finite whole
number above zero, a type that is not call or put, a style it does not know, a
date that is not a date) is stored empty, and a row with an empty field is never
standard. Only a row that is not a JSON object or has no ticker, and a ticker
listed twice, fail the listing.

A listing is admitted only if it is whole: the fetcher either returns every
page or raises, and an empty answer is refused, not pinned.

``read_option_listing`` reads only bytes the catalog admits, so it needs the catalog
reachable, the lake root's identity marker and a thread with no running event loop
(its docstring says what each costs a caller). A listing lives under the underlying's
ticker lowered (``brk.b``), where a contract's bars live under its OCC root lowered
(``brkb``, ``option_bars``); the two spellings are both right.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
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
    option_read_root,
    publish_option_artifact,
    refusal_before_claim,
    stored_file_still_reads,
    vendor_reason,
)
from app.data_lake.path_policy import OptionListingPath
from app.data_lake.polygon_fetcher import PolygonFetchError
from app.data_lake.polygon_option_contracts import PolygonOptionContract, fetch_option_contracts
from app.data_lake.polygon_option_http import OPTION_TICKER_PREFIX
from app.data_lake.types import ArtifactIdentity, ArtifactRecord, is_lake_addressable_symbol
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options.contract import OptionContract, strike_thousandths
from app.options.expiry import option_expiry_anchor_ms

logger = logging.getLogger(__name__)

OPTION_LISTING_ENDPOINT = "v3/reference/options/contracts"
#: Bump when the file's columns or encoding change. The format is part of a listing's
#: recipe, so a new format is a new catalog row: capturing a date again under it
#: publishes over the old file and stales the old format's row for that file
#: (``mark_metadata_artifacts_stale_for_path``). A date not captured again keeps its
#: old row and file until it is.
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
_CONTRACT_TYPES = frozenset({"call", "put"})
_EXERCISE_STYLES = frozenset({"american", "european", "bermudan"})
_VENDOR_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: A share count has fewer digits than this; one with more is no share count (and is
#: not turned into an integer, which a huge exponent would make very large).
_MAX_SHARE_DIGITS = 18


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
    ``contract_type`` is empty, and ``strike_thousandths`` and ``expiry_ms`` are
    ``None``, when the vendor's value could not be stored. ``contract`` is the
    :class:`OptionContract` the ticker names, set only when the ticker parses and
    agrees with the row's own type, strike and expiry; ``standard`` is the rule in
    :func:`_classify`, and a standard row always has a contract.
    """

    ticker: str
    contract_type: str
    strike_thousandths: int | None
    expiry_ms: int | None
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


def _require_ticker(underlying: str) -> None:
    """A caller's mistake, not a missing listing: the lake spells an underlying in upper case, as the catalog does."""
    if not is_lake_addressable_symbol(underlying):
        raise ValueError(f"underlying must be an upper-case ticker, got {underlying!r}")


def _member_name(underlying: str, as_of: date) -> str:
    return f"{as_of:%Y%m%d}_{underlying.lower()}_contracts.csv"


def _occ_root(underlying: str) -> str:
    """The root an OCC symbol writes for a stock: its letters (``BRK.B`` is ``BRKB``)."""
    return underlying.replace(".", "")


def _normal_ticker(ticker: str) -> str:
    """A ticker as the catalog writes a contract: without the vendor's ``O:``."""
    return ticker.removeprefix(OPTION_TICKER_PREFIX)


def _strike_cell(strike: Decimal | None) -> str:
    if strike is None or not strike.is_finite() or strike <= 0:
        return ""
    try:
        return str(strike_thousandths(strike))
    except ValueError:  # finer than a thousandth, or not an everyday size
        return ""


def _expiry_cell(text: str | None) -> str:
    if text is None or _VENDOR_DATE.fullmatch(text) is None:
        return ""
    try:
        expiry = date.fromisoformat(text)
        option_expiry_anchor_ms(expiry)  # the reader computes this; a date it could not anchor is not storable
    except (ValueError, OverflowError):
        return ""
    return f"{expiry.year:04d}{expiry.month:02d}{expiry.day:02d}"


def _shares_cell(shares: Decimal | None) -> str:
    if (
        shares is None
        or not shares.is_finite()
        or shares <= 0
        or shares.adjusted() >= _MAX_SHARE_DIGITS
        or shares != shares.to_integral_value()
    ):
        return ""
    return str(int(shares))


def _csv_line(contract: PolygonOptionContract) -> list[str]:
    """One contract as the file's cells; a value the file cannot hold is an empty cell, and never raises."""
    contract_type = (contract.contract_type or "").lower()
    exercise_style = (contract.exercise_style or "").lower()
    return [
        contract.ticker,
        contract_type if contract_type in _CONTRACT_TYPES else "",
        _strike_cell(contract.strike),
        _expiry_cell(contract.expiration_date),
        exercise_style if exercise_style in _EXERCISE_STYLES else "",
        _shares_cell(contract.shares_per_contract),
        "1" if contract.has_additional_underlyings else "0",
    ]


def build_option_listing_zip_bytes(underlying: str, as_of: date, contracts: Sequence[PolygonOptionContract]) -> bytes:
    """The listing's file. Deterministic: the same contracts, in any order, make the same bytes.

    A value the file cannot hold is stored empty, not repaired and not fatal. A
    ticker listed twice (``O:X`` and ``X`` are the same ticker) raises
    :class:`OptionListingFileError` naming it, and no file is made.
    """
    seen: set[str] = set()
    for contract in contracts:
        key = _normal_ticker(contract.ticker)
        if key in seen:
            raise OptionListingFileError(f"duplicate ticker {contract.ticker} in the listing")
        seen.add(key)
    lines = sorted((_csv_line(c) for c in contracts), key=lambda line: line[0])
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
    strike: int | None,
    expiry_ms: int | None,
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
        named = OptionContract.from_occ_symbol(_normal_ticker(ticker))
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
            strike = int(strike_text) if strike_text else None
            expiry_ms = option_expiry_anchor_ms(datetime.strptime(expiry_text, "%Y%m%d").date()) if expiry_text else None
            shares = int(shares_text) if shares_text else None
            additional = {"0": False, "1": True}[additional_text]
        except (ValueError, KeyError) as exc:
            raise OptionListingFileError(f"{expected} line {number}: {exc!r}") from exc
        # The writer stores both in lower case; a cell in another case still names the same thing.
        contract_type = contract_type.lower()
        style = style_text.lower() or None
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
    lake_root: Path | None = None,
) -> OptionListing | None:
    """The listing captured for an underlying as of a date, or ``None`` when that date has no file.

    Every row the vendor listed is here, marked standard or not (:func:`_classify`). The
    ``underlying`` must be spelled as the lake spells it, an upper-case ticker (``ValueError``).

    What a caller must have. The bytes are read only through ``admission``, which by default
    asks the catalog for the committed receipt of exactly these bytes. So:

    * the catalog must be reachable, or ``catalog_client.CatalogUnavailableError`` is raised
      (a research worker that owns no catalog connection passes an ``admission`` that asks its
      parent);
    * the lake root must carry its identity marker (``<base>/lake/.data-root.json``), or the
      bytes are refused;
    * the thread must have no running event loop (call it with ``asyncio.to_thread``): on an
      event-loop thread it raises ``RuntimeError``. A date with no file answers ``None`` before
      admission is asked, even on the wrong thread, so that error first shows on a captured date.

    What it raises. ``LakeAdmissionError``: the file exists but the catalog does not admit these
    bytes; capture the listing again. :class:`OptionListingFileError`: the admitted file is not
    what the lake writes. A date whose row is complete but whose file has gone is ``None``, like a
    date never captured; a listing the vendor answered with nothing is never stored, so ``None``
    also covers "the vendor listed nothing" (the row's ``LastError`` tells them apart).

    ``lake_root`` reads from another raw lake root (``<base>/lake/raw``, such as an alternate
    mount of the lake) instead of the configured one; any other path is a ``ValueError``.
    Synchronous, like ``LeanMinuteDataReader.read_day``.
    """
    _require_ticker(underlying)
    relative = OptionListingPath(underlying=underlying, as_of=as_of).relative_path()
    path = option_read_root(lake_root) / Path(*relative.parts)
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
    file, publish under the lease; a typed failure on every vendor error. The refusals
    that cost no claim come first (``symbol_too_long``, ``not_a_session``,
    ``session_not_over``; ``option_lake``'s docstring says what each means).
    """
    _require_ticker(underlying)
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
        return await fail(vendor_reason(e), str(e))

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
    # A listing recipe of another file format has its own row for this same file; it no longer describes it.
    await catalog_client.mark_metadata_artifacts_stale_for_path(
        data_root_id=identity.data_root_id,
        price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
        file_path=str(rel_path),
        keep_artifact_id=held.artifact_id,
    )

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
    listing is among them. Dates that are not sessions are never listings. Both ends
    of the range are included. Like :func:`captured_option_days` it answers "is there
    a complete row" and reads no file: a date whose file has since gone is still named,
    and :func:`read_option_listing` is the judge of "readable". It needs the catalog
    reachable (``catalog_client.CatalogUnavailableError`` otherwise) and nothing else.
    An ``underlying`` that is not an upper-case ticker is a ``ValueError``.
    """
    _require_ticker(underlying)
    held = {
        record.data_contract_hash
        for record in await catalog_client.select_complete_metadata_artifacts(
            OPTION_MARKET, underlying, OPTION_PROVIDER, OPTION_PRICE_ADJUSTMENT_MODE
        )
    }
    return tuple(day for day in expected_sessions(start, end) if option_listing_dch(underlying, day) in held)
