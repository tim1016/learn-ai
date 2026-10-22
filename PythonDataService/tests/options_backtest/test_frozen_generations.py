"""A freeze retains one admitted session generation across storage transitions."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.data_lake.option_bars import SessionMemberRef
from app.data_lake.option_session import read_option_session
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionSessionPath
from app.engine.data.decoded_cache import DecodedArtifactCache
from app.options.contract import OptionContract
from app.options_backtest.frozen_inputs import FrozenRunData, freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import run_sessions
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search.snapshots import SnapshotRunData
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    raw_root,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest.setup_fixtures import leg, setup


@pytest.mark.parametrize("transition", ["container_refresh", "legacy_to_container", "container_to_legacy", "stable_partial",
                                    "stable_fallback_only", "fallback_refresh"])
@respx.mock
async def test_freeze_refuses_changed_session_sources_but_accepts_stable_partial_fallback(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, transition: str,
) -> None:
    call = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
    put = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)
    await store_listing([vendor_listing_row(call), vendor_listing_row(put)])
    await store_underlying_day(fake_catalog, tmp_lake,
        [stock_bar(FRIDAY_OPEN_MS + 4 * 60_000, "500", "500", "500", "500")])
    await store_option_day(call, [])
    if transition != "legacy_to_container":
        first = await repack_option_session("SPY", FRIDAY)
        assert first.failure is None
    await store_option_day(put, [])
    if transition == "container_to_legacy":
        assert (await repack_option_session("SPY", FRIDAY)).failure is None
    given = setup(legs=[leg(expiry={"kind": "same_day"}),
                        leg(leg_id="put", right="put", expiry={"kind": "same_day"})])
    if transition in ("stable_fallback_only", "fallback_refresh"):
        given = setup(legs=[leg(right="put", expiry={"kind": "same_day"})])
    reads: list[SessionMemberRef | None] = []
    checkpoints = []

    class ChangingData(LakeRunData):
        def option_day(self, contract: OptionContract, trading_date: date) -> Any:
            found = super().option_day(contract, trading_date)
            assert found is not None
            reads.append(found.reference)
            if len(reads) == 1:
                if transition in ("container_refresh", "legacy_to_container", "fallback_refresh"):
                    # A real publication adds the second contract under exactly
                    # the same admitted listing between the two required reads.
                    refreshed = asyncio.run(repack_option_session("SPY", FRIDAY))
                    assert refreshed.failure is None
                elif transition == "container_to_legacy":
                    (raw_root(tmp_lake) / OptionSessionPath("SPY", FRIDAY).relative_path()).unlink()
            return found

    if transition in ("stable_partial", "stable_fallback_only"):
        frozen = await asyncio.to_thread(freeze_run_inputs, given, (FRIDAY,), ChangingData("SPY"), tmp_path / "frozen")
        if transition == "stable_partial":
            assert reads[0] is not None and reads[1] is None
        else:
            assert reads[0] is None
        assert str(OptionSessionPath("SPY", FRIDAY).relative_path()) in next(iter(frozen.sessions.values())).snapshot.manifest
        actual = await asyncio.to_thread(run_sessions, given, (FRIDAY,), FrozenRunData(frozen, DecodedArtifactCache(max_bytes=1024**2)))
        assert actual == await asyncio.to_thread(run_sessions, given, (FRIDAY,), LakeRunData("SPY"))
        return

    with pytest.raises(ValueError, match="generation changed"):
        await asyncio.to_thread(freeze_run_inputs, given, (FRIDAY,), ChangingData("SPY"), tmp_path / "mixed",
                                on_checkpoint=checkpoints.append)
    assert checkpoints == []
    if transition == "fallback_refresh":
        # One leg, served by the legacy fallback: retention catches the container republished since.
        assert reads == [None]
    elif transition == "container_refresh":
        assert reads[0].listing_sha256 == reads[1].listing_sha256
        assert reads[0].container_sha256 != reads[1].container_sha256
    else:
        assert (reads[0] is None) != (reads[1] is None)
    # A fresh freeze under the resulting coherent layout remains usable.
    frozen = await asyncio.to_thread(freeze_run_inputs, given, (FRIDAY,), LakeRunData("SPY"), tmp_path / "coherent")
    actual = await asyncio.to_thread(run_sessions, given, (FRIDAY,), FrozenRunData(frozen, DecodedArtifactCache(max_bytes=1024**2)))
    assert actual == await asyncio.to_thread(run_sessions, given, (FRIDAY,), LakeRunData("SPY"))
    current = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    references = [pin.reference for pin in actual.sessions[0].pins.option_days]
    if current is None:
        assert all(ref is None for ref in references)
    else:
        assert all(ref.container_sha256 == current.sha256 for ref in references)


async def _freeze_a_published_container(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, given: Any = None,
) -> tuple[OptionContract, Any]:
    call = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
    put = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)
    await store_listing([vendor_listing_row(call), vendor_listing_row(put)])
    await store_underlying_day(fake_catalog, tmp_lake,
        [stock_bar(FRIDAY_OPEN_MS + 4 * 60_000, "500", "500", "500", "500")])
    await store_option_day(call, [])
    await store_option_day(put, [])
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    return call, await asyncio.to_thread(freeze_run_inputs, given or setup(), (FRIDAY,), LakeRunData("SPY"), tmp_path / "frozen")


def _observe_container_decodes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import weakref

    from app.data_lake import option_session

    parsed: list[Any] = []
    original = option_session.parse_option_session

    def observe(*args: Any, **kwargs: Any) -> Any:
        session = original(*args, **kwargs)
        parsed.append(weakref.ref(session))
        return session

    monkeypatch.setattr(option_session, "parse_option_session", observe)
    return parsed


@respx.mock
async def test_worker_container_decode_has_no_residency_outside_its_bounded_selected_cache(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, frozen = await _freeze_a_published_container(fake_catalog, tmp_lake, tmp_path)
    parsed = _observe_container_decodes(monkeypatch)
    cache = DecodedArtifactCache(max_bytes=1)
    assert FrozenRunData(frozen, cache).listing(FRIDAY) is not None
    assert cache.metrics()["resident_bytes"] == 0
    assert len(parsed) == 1
    assert parsed[0]() is None, "the full decoded container must die with the local read scope"


@respx.mock
async def test_snapshot_reader_releases_its_container_decode_with_the_reader(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call, frozen = await _freeze_a_published_container(fake_catalog, tmp_lake, tmp_path)
    parsed = _observe_container_decodes(monkeypatch)
    reader = SnapshotRunData("SPY", next(iter(frozen.sessions.values())).snapshot, BlobStore(Path(frozen.root)))
    assert reader.option_day(call, FRIDAY) is not None
    assert parsed[0]() is not None
    del reader
    assert len(parsed) == 1
    assert parsed[0]() is None, "the full decoded container must die with the reader"


@respx.mock
async def test_a_cold_worker_read_verifies_and_decodes_the_container_once_for_all_its_contracts(
    fake_catalog: Any, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    both_legs = setup(legs=[leg(expiry={"kind": "same_day"}), leg(leg_id="put", right="put", expiry={"kind": "same_day"})])
    _, frozen = await _freeze_a_published_container(fake_catalog, tmp_lake, tmp_path, both_legs)
    session = next(iter(frozen.sessions.values()))
    assert len(session.contracts) == 2
    container = session.snapshot.manifest[str(OptionSessionPath("SPY", FRIDAY).relative_path())]
    reads: list[str] = []
    real_get = BlobStore.get

    def counting_get(self: BlobStore, digest: str, **kwargs: Any) -> bytes:
        reads.append(digest)
        return real_get(self, digest, **kwargs)

    monkeypatch.setattr(BlobStore, "get", counting_get)
    assert FrozenRunData(frozen, DecodedArtifactCache(max_bytes=1024**2)).listing(FRIDAY) is not None
    # One hash of the whole inventory before the cache is asked, and one read of the container to decode it.
    assert reads.count(container) == 2
