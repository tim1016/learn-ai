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
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
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
    assert not session.contracts_complete
    assert session.legacy_bytes(CALL.occ_symbol) == payloads[CALL.occ_symbol]
    assert session.option_day(PUT.occ_symbol).bars == ()
    assert session.option_day(OTHER.occ_symbol) is None
    assert session.reference(CALL.occ_symbol).member_sha256 == hashlib.sha256(payloads[CALL.occ_symbol]).hexdigest()


@respx.mock
async def test_full_session_and_repack_report_require_admitted_underlying(fake_catalog, tmp_lake):
    from scripts.repack_option_sessions import repack

    listing = await listing_of(CALL)
    await store_option_day(CALL, [])
    parsed = parse_option_session(build_option_session_bytes(listing, {CALL.occ_symbol: option_day_zip(CALL, [])}), listing)
    assert parsed.contracts_complete
    [missing] = await repack(["SPY"])
    assert missing["contracts_complete"] and not missing["complete"]
    path = await store_underlying_day(fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "500", "500", "500", "500")])
    [held] = await repack(["SPY"])
    assert held["contracts_complete"] and held["complete"]
    path.write_bytes(b"unadmitted replacement")
    with pytest.raises(LakeAdmissionError):
        await repack(["SPY"])
    # Contract data stays readable independently of full-session readiness.
    assert (await asyncio.to_thread(read_option_session, "SPY", FRIDAY)).contracts_complete
    assert await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY, listed_under="SPY") is not None


@respx.mock
async def test_publication_temp_capacity_is_reserved_before_claim_and_old_temps_remain(fake_catalog, tmp_lake):
    from app.data_lake.option_session import option_session_dch
    from app.data_lake.option_session_staging import OptionSessionStaging
    from app.data_lake.path_policy import resolve_staging_root

    listing = await listing_of(CALL)
    await store_option_day(CALL, [])
    payload = build_option_session_bytes(listing, {CALL.occ_symbol: option_day_zip(CALL, [])})
    budget = OptionSessionStaging(resolve_staging_root("raw"))
    old_temp = budget.publication_root / "dead-process" / "original.zip.tmp"
    old_temp.parent.mkdir(parents=True)
    old_temp.write_bytes(b"preserve this interrupted publication")
    budget.reconcile()
    used = json.loads(budget.ledger.read_bytes())["used_bytes"]
    paused = await repack_option_session("SPY", FRIDAY, max_staging_bytes=used + len(payload) - 1)
    assert paused.failure.reason == "staging_capacity" and "staging capacity" in paused.failure.detail.lower()
    assert not any(row["data_contract_hash"] == option_session_dch() for row in fake_catalog.rows.values())
    assert old_temp.read_bytes() == b"preserve this interrupted publication"
    recovered = await repack_option_session("SPY", FRIDAY, max_staging_bytes=used + len(payload))
    assert recovered.failure is None
    assert old_temp.read_bytes() == b"preserve this interrupted publication"
    assert list(budget.publication_root.rglob("*.tmp")) == [old_temp]


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
    original = await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY, listed_under="SPY")
    result = await repack_option_session("SPY", FRIDAY)
    assert result.failure is None
    repeat = await repack_option_session("SPY", FRIDAY)
    assert repeat.reused
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.captured == {CALL.occ_symbol} and session.confirmed_empty == {PUT.occ_symbol}
    assert session.unresolved == {OTHER.occ_symbol}
    legacy = raw_root(tmp_lake) / OptionMinuteBarPath("SPY", CALL.occ_symbol, FRIDAY).relative_path()
    legacy.unlink()
    read = await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY, listed_under="SPY")
    assert read.bars == original.bars and read.sha256 == original.sha256
    assert read.reference.container_sha256 == result.record.file_sha256


@respx.mock
async def test_contract_reads_of_one_admitted_session_share_one_decode_and_every_read_is_admitted(
    fake_catalog, tmp_lake, monkeypatch
):
    from app.data_lake import option_session
    from app.data_lake.option_listing import OptionListingFileError
    from app.data_lake.path_policy import OptionListingPath

    listing = await listing_of(CALL, PUT, OTHER)
    await store_option_day(CALL, [{"t": FRIDAY_OPEN_MS, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    await store_option_day(PUT, [])
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    monkeypatch.setattr(option_session, "_LAST_SESSION", option_session.OptionSessionDecodeScope())
    decodes = []
    real_decode = option_session.parse_option_session
    monkeypatch.setattr(option_session, "parse_option_session", lambda *a, **k: decodes.append(1) or real_decode(*a, **k))
    raw, path = raw_root(tmp_lake), str(OptionSessionPath("SPY", FRIDAY).relative_path())

    def read(contract):
        return read_option_day_bars(contract, FRIDAY, listed_under="SPY")

    for _ in range(3):
        call, put, other = [await asyncio.to_thread(read, c) for c in (CALL, PUT, OTHER)]
        assert len(call.bars) == 1 and put.bars == () and other is None
    assert (await asyncio.to_thread(read_option_session, "SPY", FRIDAY, listing=listing)).captured == {CALL.occ_symbol}
    assert len(decodes) == 1

    # Other admitted container bytes are another session: decoded afresh, never the old decode.
    original = (raw / path).read_bytes()
    changed = build_option_session_bytes(listing, {CALL.occ_symbol: option_day_zip(CALL, [FRIDAY_OPEN_MS, FRIDAY_OPEN_MS + 60_000]),
                                                   PUT.occ_symbol: option_day_zip(PUT, [])})
    replace_the_stored_file(fake_catalog, raw, path, changed, admit=True)
    read_again = await asyncio.to_thread(read, CALL)
    assert len(read_again.bars) == 2 and read_again.reference.container_sha256 == hashlib.sha256(changed).hexdigest()
    assert len(decodes) == 2
    # A resident decode never stands in for admission: bytes the receipt does not name are refused.
    replace_the_stored_file(fake_catalog, raw, path, original, admit=False)
    with pytest.raises(LakeAdmissionError):
        await asyncio.to_thread(read, CALL)
    # The listing is admitted on every read too: other listing bytes miss the memo, so they are parsed and refused.
    replace_the_stored_file(fake_catalog, raw, path, changed, admit=True)
    listing_path = str(OptionListingPath("SPY", FRIDAY).relative_path())
    replace_the_stored_file(fake_catalog, raw, listing_path, b"a refreshed listing", admit=True)
    with pytest.raises(OptionListingFileError):
        await asyncio.to_thread(read, CALL)


@respx.mock
async def test_published_corrupt_session_never_falls_back_to_good_legacy(fake_catalog, tmp_lake):
    await listing_of(CALL)
    await store_option_day(CALL, [])
    await repack_option_session("SPY", FRIDAY)
    path = str(OptionSessionPath("SPY", FRIDAY).relative_path())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, b"corrupt", admit=True)
    with pytest.raises(OptionSessionFileError):
        await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY, listed_under="SPY")
    with pytest.raises(OptionSessionFileError):
        await repack_option_session("SPY", FRIDAY, rebind_listing=True)
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, b"different", admit=False)
    with pytest.raises(LakeAdmissionError):
        await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY, listed_under="SPY")


@respx.mock
async def test_ten_thousand_listing_repack_keeps_26_known_days_partial(fake_catalog, tmp_lake):
    contracts = [OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=400 + i) for i in range(10_000)]
    await listing_of(*contracts)
    for contract in contracts[:26]:
        await store_option_day(contract, [])
    outcome = await repack_option_session("SPY", FRIDAY)
    assert outcome.failure is None
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert not session.contracts_complete and session.answered_count == 26 and len(session.unresolved) == 9_974
    assert session.confirmed_empty == {contract.occ_symbol for contract in contracts[:26]}
    assert await asyncio.to_thread(read_option_day_bars, contracts[0], FRIDAY, listed_under="SPY") is not None
    assert await asyncio.to_thread(read_option_day_bars, contracts[-1], FRIDAY, listed_under="SPY") is None


@respx.mock
async def test_late_snapshot_cannot_replace_newer_resolved_container_member(fake_catalog, tmp_lake):
    from app.data_lake.option_session_repack import admitted_legacy_members, publish_option_session

    listing = await listing_of(CALL, PUT)
    await store_option_day(CALL, [])
    stale = await asyncio.to_thread(admitted_legacy_members, listing)
    relative = str(OptionMinuteBarPath("SPY", CALL.occ_symbol, FRIDAY).relative_path())
    refreshed = option_day_zip(CALL, [FRIDAY_OPEN_MS])
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), relative, refreshed, admit=True)
    winner = await publish_option_session(listing, stale)
    late = await publish_option_session(listing, stale)
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert late.reused and late.record.file_sha256 == winner.record.file_sha256
    assert session.legacy_bytes(CALL.occ_symbol) == refreshed
    # Retained legacy originals may be older than a container. Neither capture
    # startup nor a subsequent offline repack may make them win again.
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), relative, stale[CALL.occ_symbol], admit=True)
    repeated = await repack_option_session("SPY", FRIDAY)
    assert repeated.reused
    assert (await asyncio.to_thread(read_option_session, "SPY", FRIDAY)).legacy_bytes(CALL.occ_symbol) == refreshed

    from app.options_backtest.option_session_capture import capture_option_session, read_session_completeness
    from tests._helpers.option_vendor import no_trades
    from tests.options_backtest._lake import NOW_2026_10_12_MS, stock_bar, store_underlying_day
    await store_underlying_day(fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "500", "500", "500", "500")])
    respx.get(f"https://api.polygon.io/v2/aggs/ticker/O:{PUT.occ_symbol}/range/1/minute/{FRIDAY}/{FRIDAY}").mock(
        return_value=no_trades(PUT.occ_symbol))
    native = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert native.failure is None
    after_capture, complete = await asyncio.to_thread(read_session_completeness, "SPY", FRIDAY)
    assert complete and after_capture.legacy_bytes(CALL.occ_symbol) == refreshed


@pytest.mark.parametrize("escape", ["root", "underlying", "lock_file"])
def test_publication_lock_refuses_symlink_escape_before_opening_outside_file(tmp_lake, tmp_path, escape):
    from app.data_lake.option_lake import option_read_root, option_session_publication_lock
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    lock_root = option_read_root().parent.parent / ".option-session-publication"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"")
    if escape == "root":
        lock_root.symlink_to(outside, target_is_directory=True)
    elif escape == "underlying":
        lock_root.mkdir(parents=True)
        (lock_root / "spy").symlink_to(outside, target_is_directory=True)
    else:
        (lock_root / "spy").mkdir(parents=True)
        (lock_root / "spy" / f".{session_open_ms_utc(FRIDAY)}.lock").symlink_to(sentinel)
    with pytest.raises(ValueError, match="escapes root"), option_session_publication_lock("SPY", FRIDAY):
        pytest.fail("an escaping lock must never be acquired")
    assert list(outside.iterdir()) == [sentinel] and sentinel.read_bytes() == b""
