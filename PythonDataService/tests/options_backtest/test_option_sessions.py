"""Session coverage and original-byte pin compatibility through real lake files."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from uuid import uuid4

import pytest
import respx

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_bars import read_option_day_bars
from app.data_lake.option_listing import read_option_listing
from app.data_lake.option_session import (
    OptionSessionFileError,
    build_option_session_bytes,
    parse_option_session,
    read_option_session,
)
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionMinuteBarPath, OptionSessionPath
from app.options.contract import OptionContract
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    option_day_zip,
    raw_root,
    replace_the_stored_file,
    store_listing,
    store_option_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

CALL = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
PUT = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)
OTHER = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=501)


async def listing_of(*contracts):
    await store_listing([vendor_listing_row(c) for c in contracts])
    return await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)


def mutate(payload, change):
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members["manifest.json"])
    change(members, manifest)
    members["manifest.json"] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for name, raw in members.items():
            target.writestr(name, raw)
    return out.getvalue()


@respx.mock
async def test_coverage_determinism_empty_and_exact_original_bytes(fake_catalog, tmp_lake):
    listing = await listing_of(CALL, PUT, OTHER)
    payloads = {CALL.occ_symbol: option_day_zip(CALL, [FRIDAY_OPEN_MS]),
                PUT.occ_symbol: option_day_zip(PUT, [])}
    payload = build_option_session_bytes(listing, payloads)
    assert payload == build_option_session_bytes(listing, dict(reversed(list(payloads.items()))))
    session = parse_option_session(payload, listing)
    assert session.captured == {CALL.occ_symbol}
    assert session.confirmed_empty == {PUT.occ_symbol}
    assert session.unresolved == {OTHER.occ_symbol}
    assert not session.complete
    assert session.legacy_bytes(CALL.occ_symbol) == payloads[CALL.occ_symbol]
    assert session.option_day(PUT.occ_symbol).bars == ()
    assert session.option_day(OTHER.occ_symbol) is None
    assert session.reference(CALL.occ_symbol)["member_sha256"] == hashlib.sha256(payloads[CALL.occ_symbol]).hexdigest()


@pytest.mark.parametrize("damage", ["listing", "recipe", "unknown", "unsafe", "missing", "count", "overlap", "corrupt_unrequested"])
@respx.mock
async def test_entire_container_refuses_invalid_members_or_coverage(fake_catalog, tmp_lake, damage):
    listing = await listing_of(CALL, PUT)
    original = build_option_session_bytes(listing, {CALL.occ_symbol: option_day_zip(CALL, [FRIDAY_OPEN_MS]),
                                                  PUT.occ_symbol: option_day_zip(PUT, [])})
    def change(members, manifest):
        if damage == "listing":
            manifest["listing_sha256"] = "0" * 64
        elif damage == "recipe":
            manifest["recipe"] = "0" * 64
        elif damage == "unknown":
            members["unasked.zip"] = b"unknown"
        elif damage == "unsafe":
            members["../escape"] = b"unknown"
        elif damage == "missing":
            members.pop(f"contracts/{PUT.occ_symbol}.zip")
        elif damage == "count":
            manifest["answered_count"] += 1
        elif damage == "overlap":
            manifest["unresolved"].append(CALL.occ_symbol)
        else:
            members[f"contracts/{PUT.occ_symbol}.zip"] = b"corrupt but caller only wanted CALL"
    with pytest.raises(OptionSessionFileError):
        parse_option_session(mutate(original, change), listing)
    with pytest.raises(OptionSessionFileError, match="identity mismatch"):
        parse_option_session(original, listing, root_id=uuid4())


@respx.mock
async def test_duplicate_members_and_nested_decoded_byte_limit_refuse(fake_catalog, tmp_lake, monkeypatch):
    listing = await listing_of(CALL)
    payload = build_option_session_bytes(listing, {CALL.occ_symbol: option_day_zip(CALL, [FRIDAY_OPEN_MS])})
    out = io.BytesIO(payload)
    with zipfile.ZipFile(out, "a") as archive, pytest.warns(UserWarning, match="Duplicate"):
        archive.writestr("manifest.json", b"{}")
    with pytest.raises(OptionSessionFileError):
        parse_option_session(out.getvalue(), listing)
    monkeypatch.setattr("app.data_lake.option_session.MAX_TICKER_DECODED_BYTES", 1)
    with pytest.raises(OptionSessionFileError, match="invalid ticker member"):
        parse_option_session(payload, listing)


@respx.mock
async def test_partial_repack_preserves_original_hash_and_empty_state_and_is_idempotent(fake_catalog, tmp_lake):
    await listing_of(CALL, PUT, OTHER)
    await store_option_day(CALL, [{"t": FRIDAY_OPEN_MS, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    await store_option_day(PUT, [])
    original = await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY)
    result = await repack_option_session("SPY", FRIDAY)
    assert result.failure is None
    repeat = await repack_option_session("SPY", FRIDAY)
    assert repeat.reused
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.captured == {CALL.occ_symbol} and session.confirmed_empty == {PUT.occ_symbol}
    assert session.unresolved == {OTHER.occ_symbol}
    legacy = raw_root(tmp_lake) / OptionMinuteBarPath("SPY", CALL.occ_symbol, FRIDAY).relative_path()
    legacy.unlink()
    read = await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY)
    assert read.bars == original.bars and read.sha256 == original.sha256
    assert read.reference["container_sha256"] == result.record.file_sha256


@respx.mock
async def test_published_corrupt_session_never_falls_back_to_good_legacy(fake_catalog, tmp_lake):
    await listing_of(CALL)
    await store_option_day(CALL, [])
    await repack_option_session("SPY", FRIDAY)
    path = str(OptionSessionPath("SPY", FRIDAY).relative_path())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, b"corrupt", admit=True)
    with pytest.raises(OptionSessionFileError):
        await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY)
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, b"different", admit=False)
    with pytest.raises(LakeAdmissionError):
        await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY)
