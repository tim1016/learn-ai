"""GET /api/scoped-health: the lake catalog, option archive, backups and IBKR feed fail one at a time (#3500, T20).

Each test runs the real route and the real projection over fake source readers,
so what is asserted is what a page would be told: which items exist, how bad
they are, which operations they get in the way of, and where the evidence is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.routers.scoped_health import router
from app.services import scoped_health_service
from app.services.scoped_health_service import HealthReaders, ScopedHealthService, get_scoped_health_service
from tests._helpers.scoped_health import (
    EARLIER_SESSION_OPEN_MS,
    NOW_MS,
    SESSION_OPEN_MS,
    FakeClock,
    archive_health,
    failing,
    ibkr_health,
    job,
    lake_readiness,
    readers,
    serving,
    symbol_row,
    underlying,
)

HEALTHY_SOURCES = {"lake_catalog", "option_archive", "backups", "ibkr_feed"}


async def get_health(health_readers: HealthReaders, **params: Any) -> dict[str, Any]:
    app = FastAPI()
    app.include_router(router)
    service = ScopedHealthService(health_readers, clock=FakeClock())
    app.dependency_overrides[get_scoped_health_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/scoped-health", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_kind(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    kinds: dict[str, list[dict[str, Any]]] = {}
    for item in body["items"]:
        kinds.setdefault(item["scope"]["data_kind"], []).append(item)
    return kinds


def attention(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in body["items"] if item["needs_attention"]]


async def test_when_every_source_is_healthy_each_one_says_so_with_when_it_was_read() -> None:
    body = await get_health(readers())

    assert {item["source"] for item in body["items"]} == HEALTHY_SOURCES
    assert attention(body) == []
    assert {item["severity"] for item in body["items"]} == {"ok"}
    assert {item["freshness"] for item in body["items"]} == {"fresh"}
    assert body["generated_ms"] == NOW_MS
    assert all(isinstance(item["observed_ms"], int) for item in body["items"])


@pytest.mark.parametrize(
    ("failed", "unknown_kinds"),
    [
        ("lake", {"stock_bars"}),
        ("archive", {"option_archive", "lake_backup", "restore_check"}),
        ("ibkr", {"ibkr_market_data"}),
    ],
)
async def test_a_source_that_fails_reads_unknown_and_leaves_the_others_as_they_were(
    failed: str, unknown_kinds: set[str]
) -> None:
    body = await get_health(readers(**{failed: failing()}))

    kinds = by_kind(body)
    for kind, items in kinds.items():
        if kind in unknown_kinds:
            assert [(item["severity"], item["freshness"], item["needs_attention"]) for item in items] == [
                ("unknown", "unknown", True)
            ], kind
        else:
            assert {item["severity"] for item in items} == {"ok"}, kind
    # Nothing the failed source owns is missing, and none of it passes for healthy.
    assert unknown_kinds <= kinds.keys()


async def test_a_source_that_never_answers_reads_unknown_instead_of_holding_the_page_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scoped_health_service, "SOURCE_TIMEOUT_S", 0.05)

    async def hangs() -> None:
        await asyncio.sleep(60)

    body = await get_health(readers(ibkr=hangs))

    assert [(item["severity"], item["freshness"]) for item in by_kind(body)["ibkr_market_data"]] == [("unknown", "unknown")]
    assert {item["severity"] for kind, items in by_kind(body).items() if kind != "ibkr_market_data" for item in items} == {"ok"}


async def test_the_worst_finding_comes_first_and_an_unknown_outranks_a_warning() -> None:
    body = await get_health(
        readers(
            archive=serving(archive_health(underlyings=[underlying("SPY", "at_risk", "Gaps need attention.", gaps=2)])),
            ibkr=failing(),
            lake=serving(lake_readiness(symbol_row("MSFT", missing=1))),
        )
    )

    assert [item["severity"] for item in body["items"]][:3] == ["unknown", "warning", "warning"]
    assert [item["severity"] for item in body["items"]] == sorted(
        (item["severity"] for item in body["items"]), key=["critical", "unknown", "warning", "info", "ok"].index
    )


# ── Archive and backups ──────────────────────────────────────────────────────


async def test_a_backup_failure_is_about_recovery_and_flags_no_chart_quote_or_live_feed() -> None:
    health = archive_health(backup=job("failed", "The configured backup destination is unavailable."))

    everything = await get_health(readers(archive=serving(health)))
    [backup] = [item for item in attention(everything)]
    assert (backup["scope"]["data_kind"], backup["severity"], backup["affects"]) == ("lake_backup", "critical", ["recovery"])
    assert backup["link"] == {"tab": "backups", "query": {}, "fragment": "option-archive-backup"}
    assert "recoverability only" in backup["explanation"]

    for operations in (["stock_history"], ["options_history"], ["live_chain", "bot_feed"]):
        asked = await get_health(readers(archive=serving(health)), operation=operations, instrument="SPY")
        assert attention(asked) == [], operations
    recovery = await get_health(readers(archive=serving(health)), operation="recovery")
    assert [item["scope"]["data_kind"] for item in recovery["items"]] == ["lake_backup", "restore_check"]


async def test_a_restore_check_that_is_behind_links_to_its_own_card_on_the_backups_tab() -> None:
    body = await get_health(readers(archive=serving(archive_health(restore=job("at_risk", "The last test restore is stale.")))))

    [stale] = attention(body)
    assert (stale["scope"]["data_kind"], stale["severity"]) == ("restore_check", "warning")
    assert stale["link"] == {"tab": "backups", "query": {}, "fragment": "option-archive-restore"}


async def test_an_archive_gap_names_its_underlying_and_first_session_and_leaves_recovery_and_the_chain_alone() -> None:
    health = archive_health(underlyings=[underlying("SPY", "at_risk", "Archive gaps need attention.", gaps=3), underlying("QQQ")])

    body = await get_health(readers(archive=serving(health)))

    [gap] = attention(body)
    assert (gap["scope"]["data_kind"], gap["scope"]["instrument"], gap["scope"]["breadth"]) == ("option_archive", "SPY", "scoped")
    assert gap["scope"]["session_ms"] == SESSION_OPEN_MS
    assert gap["affects"] == ["options_history"]
    assert gap["link"] == {"tab": "option-archive", "query": {}, "fragment": "option-archive-SPY"}
    assert gap["observed_ms"] == NOW_MS

    qqq = await get_health(readers(archive=serving(health)), operation="options_history", instrument="QQQ")
    assert [(item["scope"]["instrument"], item["severity"]) for item in qqq["items"]] == [("QQQ", "ok")]


async def test_an_archive_that_is_still_catching_up_is_shown_but_does_not_ask_for_attention() -> None:
    body = await get_health(
        readers(archive=serving(archive_health(underlyings=[underlying("SPY", "catching_up", "Catching up, oldest first.", gaps=4)])))
    )

    [spy] = [item for item in body["items"] if item["scope"]["data_kind"] == "option_archive"][:1]
    assert (spy["severity"], spy["needs_attention"]) == ("info", False)


async def test_a_backup_with_no_record_is_unknown_not_fine() -> None:
    body = await get_health(readers(archive=serving(archive_health(backup=job("unavailable", "No checked backup has been recorded.")))))

    [backup] = attention(body)
    assert (backup["scope"]["data_kind"], backup["severity"]) == ("lake_backup", "unknown")


async def test_a_finding_that_cannot_name_its_instruments_bears_on_every_instrument_a_page_asks_about() -> None:
    backup_down = readers(archive=serving(archive_health(backup=job("failed", "The destination is unavailable."))))
    recovery = await get_health(backup_down, operation="recovery", instrument="SPY")
    assert [(item["scope"]["breadth"], item["severity"]) for item in attention(recovery)] == [("broad", "critical")]

    catalog_down = await get_health(readers(lake=failing()), operation="stock_history", instrument="SPY")
    assert [(item["scope"]["breadth"], item["severity"]) for item in attention(catalog_down)] == [("broad", "unknown")]


# ── The IBKR feed ────────────────────────────────────────────────────────────


async def test_ibkr_down_gets_in_the_way_of_the_live_chain_and_bot_feed_only() -> None:
    down = readers(ibkr=serving(ibkr_health("hard_down")))

    everything = await get_health(down)
    [feed] = attention(everything)
    assert (feed["scope"]["data_kind"], feed["severity"], feed["affects"]) == (
        "ibkr_market_data",
        "critical",
        ["live_chain", "bot_feed"],
    )
    assert feed["scope"]["breadth"] == "broad" and feed["scope"]["mode"] == "paper"
    assert feed["link"] is None
    assert "archived history is not" in feed["explanation"]

    for operations in (["stock_history"], ["options_history"], ["recovery"], ["options_history", "recovery"]):
        assert attention(await get_health(down, operation=operations)) == [], operations
    for operations in (["live_chain"], ["bot_feed"]):
        assert [item["severity"] for item in (await get_health(down, operation=operations))["items"]] == ["critical"]


async def test_a_feed_that_is_switched_off_is_reported_in_the_brokers_own_words() -> None:
    body = await get_health(readers(ibkr=serving(ibkr_health("disabled"))))

    [feed] = [item for item in body["items"] if item["source"] == "ibkr_feed"]
    assert feed["summary"] == "Data-plane broker disabled"
    assert feed["needs_attention"] is False


async def test_a_coordinator_with_no_gateway_session_reads_unknown_for_the_feed_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scoped_health_service.fleet_settings, "ROLE", "fleet_coordinator")

    body = await get_health(readers(ibkr=scoped_health_service._read_ibkr))

    [feed] = [item for item in body["items"] if item["source"] == "ibkr_feed"]
    assert (feed["severity"], feed["freshness"]) == ("unknown", "unknown")
    assert feed["explanation"].startswith("This data service holds no IBKR session")


# ── The lake catalog ─────────────────────────────────────────────────────────


async def test_a_lake_gap_is_scoped_to_its_symbol_and_links_to_the_lake_tab_filtered_to_it() -> None:
    lake = readers(lake=serving(lake_readiness(symbol_row("SPY"), symbol_row("MSFT", missing=1), symbol_row("GLD", unreadable=40))))

    msft = await get_health(lake, operation="stock_history", instrument="MSFT")

    [gap] = msft["items"]
    assert (gap["scope"]["data_kind"], gap["scope"]["instrument"], gap["scope"]["mode"], gap["severity"]) == (
        "stock_bars",
        "MSFT",
        "polygon_split_adjusted",
        "warning",
    )
    assert gap["affects"] == ["stock_history"]
    assert gap["link"] == {
        "tab": "lake",
        "query": {"symbol": "MSFT", "price_adjustment_mode": "polygon_split_adjusted", "data_type": "trade"},
        "fragment": None,
    }
    assert "1 session" in gap["summary"] and "missing" in gap["summary"]

    # The other symbols' problems are none of this page's business; a clean symbol says so.
    spy = await get_health(lake, operation="stock_history", instrument="SPY")
    assert [(item["scope"]["instrument"], item["severity"], item["needs_attention"]) for item in spy["items"]] == [("SPY", "ok", False)]
    # A symbol the lake does not hold has no lake finding at all.
    assert (await get_health(lake, operation="stock_history", instrument="NVDA"))["items"] == []


async def test_sessions_a_run_refuses_and_sessions_missing_are_both_said_for_one_symbol() -> None:
    body = await get_health(
        readers(lake=serving(lake_readiness(symbol_row("GLD", unreadable=40, missing=2)))), operation="stock_history", instrument="GLD"
    )

    [gld] = body["items"]
    assert gld["summary"] == "GLD: 40 sessions a run refuses, 2 sessions missing"
    # Where the earliest affected session is: the first one a rebuild would redo, before the first gap.
    assert gld["scope"]["session_ms"] == EARLIER_SESSION_OPEN_MS


async def test_a_long_list_of_lake_problems_is_cut_and_the_rest_folded_into_one_broad_item() -> None:
    rows = [symbol_row(f"S{index:02d}", missing=1) for index in range(25)]

    body = await get_health(readers(lake=serving(lake_readiness(*rows))), operation="stock_history")

    scoped = [item for item in body["items"] if item["scope"]["breadth"] == "scoped"]
    [folded] = [item for item in body["items"] if item["scope"]["breadth"] == "broad"]
    assert len(scoped) == 20
    assert folded["summary"] == "5 more symbols need attention"
    assert folded["link"] == {"tab": "lake", "query": {}, "fragment": None}
    # A page that asks about one of the folded symbols still gets that symbol's own item.
    last = await get_health(readers(lake=serving(lake_readiness(*rows))), operation="stock_history", instrument="S24")
    assert [item["scope"]["instrument"] for item in last["items"]] == ["S24"]


async def test_a_lake_with_no_problem_is_one_broad_ok_item() -> None:
    body = await get_health(readers(), operation="stock_history")

    [lake] = body["items"]
    assert (lake["severity"], lake["scope"]["breadth"], lake["scope"]["instrument"]) == ("ok", "broad", None)


# ── What a request reads ─────────────────────────────────────────────────────


async def test_a_request_reads_only_the_sources_it_can_be_about() -> None:
    reads: list[str] = []

    def counting(name: str, value: Any) -> Callable[[], Any]:
        async def read() -> Any:
            reads.append(name)
            return value

        return read

    counted = readers(
        lake=counting("lake", lake_readiness()), archive=counting("archive", archive_health()), ibkr=counting("ibkr", ibkr_health())
    )

    await get_health(counted, operation="live_chain")
    assert reads == ["ibkr"]
    reads.clear()
    await get_health(counted, operation=["stock_history", "recovery"])
    assert sorted(reads) == ["archive", "lake"]


@pytest.mark.parametrize("params", [{"instrument": "spy"}, {"instrument": "SPY; DROP"}, {"operation": "everything"}])
async def test_a_malformed_filter_is_refused(params: dict[str, str]) -> None:
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/scoped-health", params=params)
    assert response.status_code == 422


def test_the_projection_is_mounted_on_the_data_plane() -> None:
    from app.main import app

    assert "/api/scoped-health" in {route.path for route in app.routes}
