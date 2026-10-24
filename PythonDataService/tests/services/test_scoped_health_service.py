"""The scoped-health projection: how sources are read and reused, and how their own words are mapped (#3500)."""

from __future__ import annotations

import asyncio
from typing import get_args

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.schemas.option_archive import ArchiveSeverity
from app.schemas.scoped_health import HealthItem
from app.services.scoped_health_items import REASON_LIMIT
from app.services.scoped_health_service import (
    LAKE_FAILURE_TTL_MS,
    LAKE_TTL_MS,
    HealthReaders,
    ScopedHealthService,
    SourceUnreadable,
    get_scoped_health_service,
)
from tests._helpers.scoped_health import (
    FakeClock,
    archive_health,
    failing,
    job,
    lake_readiness,
    readers,
    serving,
    underlying,
)


def lake_of(items: list[HealthItem]) -> list[HealthItem]:
    return [item for item in items if item.scope.data_kind == "stock_bars"]


class CountingLake:
    """A lake reader that counts its reads and can be made to fail."""

    def __init__(self) -> None:
        self.reads = 0
        self.fail = False

    async def __call__(self):
        self.reads += 1
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("the catalog is down")
        return lake_readiness()


async def test_a_lake_reading_is_reused_for_its_window_then_read_again() -> None:
    clock, lake = FakeClock(), CountingLake()
    service = ScopedHealthService(readers(lake=lake), clock=clock)

    first = await service.project()
    clock.advance(LAKE_TTL_MS - 1)
    second = await service.project()
    clock.advance(1)
    third = await service.project()

    assert lake.reads == 2
    assert [item.freshness for item in lake_of(first.items)] == ["fresh"]
    assert [item.freshness for item in lake_of(second.items)] == ["cached"]
    assert [item.freshness for item in lake_of(third.items)] == ["fresh"]
    # A cached reading keeps the time it was read, not the time it was served.
    assert lake_of(second.items)[0].observed_ms == lake_of(first.items)[0].observed_ms


async def test_requests_that_arrive_together_share_one_lake_read() -> None:
    lake = CountingLake()
    service = ScopedHealthService(readers(lake=lake), clock=FakeClock())

    await asyncio.gather(*(service.project() for _ in range(5)))

    assert lake.reads == 1


async def test_a_failed_lake_read_is_tried_again_soon_and_a_recovery_shows() -> None:
    clock, lake = FakeClock(), CountingLake()
    lake.fail = True
    service = ScopedHealthService(readers(lake=lake), clock=clock)

    down = await service.project()
    await service.project()
    assert lake.reads == 1
    assert [item.severity for item in lake_of(down.items)] == ["unknown"]

    lake.fail = False
    clock.advance(LAKE_FAILURE_TTL_MS)
    recovered = await service.project()

    assert lake.reads == 2
    assert [(item.severity, item.freshness) for item in lake_of(recovered.items)] == [("ok", "fresh")]


async def test_the_archive_and_the_feed_are_asked_every_time() -> None:
    reads = {"archive": 0, "ibkr": 0}

    def counting(name: str, value: object):
        async def read():
            reads[name] += 1
            return value

        return read

    base = readers()
    service = ScopedHealthService(
        HealthReaders(lake=base.lake, archive=counting("archive", archive_health()), ibkr=counting("ibkr", await base.ibkr())),
        clock=FakeClock(),
    )

    await service.project()
    await service.project()

    assert reads == {"archive": 2, "ibkr": 2}


def test_the_archive_severity_table_below_covers_every_severity_the_archive_can_report() -> None:
    assert set(get_args(ArchiveSeverity)) == {"healthy", "catching_up", "waiting", "at_risk", "failed", "unavailable"}


@pytest.mark.parametrize(
    ("archive_severity", "severity", "needs_attention"),
    [
        ("healthy", "ok", False),
        ("catching_up", "info", False),
        ("waiting", "info", False),
        ("at_risk", "warning", True),
        ("failed", "critical", True),
        ("unavailable", "unknown", True),
    ],
)
async def test_every_archive_severity_maps_to_one_scoped_severity(
    archive_severity: ArchiveSeverity, severity: str, needs_attention: bool
) -> None:
    health = archive_health(underlyings=[underlying("SPY", archive_severity)], backup=job(archive_severity), restore=job(archive_severity))

    response = await ScopedHealthService(readers(archive=serving(health)), clock=FakeClock()).project(
        operations=frozenset({"options_history", "recovery"})
    )

    assert {(item.severity, item.needs_attention) for item in response.items} == {(severity, needs_attention)}
    assert len(response.items) == 3


async def test_a_long_vendor_reason_is_cut_before_it_reaches_a_page() -> None:
    vendor = "Polygon 403 for O:SPY241231P00472000: " + "upgrade your plan " * 40
    health = archive_health(underlyings=[underlying("SPY", "failed", vendor)])

    response = await ScopedHealthService(readers(archive=serving(health)), clock=FakeClock()).project(
        operations=frozenset({"options_history"}), instrument="SPY"
    )

    [item] = response.items
    assert item.summary.startswith("SPY: Polygon 403") and item.summary.endswith("...")
    assert len(item.summary) <= len("SPY: ") + REASON_LIMIT


async def test_a_source_that_says_why_it_cannot_answer_has_its_sentence_shown() -> None:
    reason = "The option archive is not running in this data service, so its status cannot be read."

    response = await ScopedHealthService(readers(archive=failing(SourceUnreadable(reason))), clock=FakeClock()).project(
        operations=frozenset({"options_history"})
    )

    [item] = response.items
    assert (item.severity, item.freshness) == ("unknown", "unknown")
    assert item.explanation.startswith(reason)


# ── The default readers ──────────────────────────────────────────────────────


def request_to(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": []})


class StubArchive:
    def health(self):
        return archive_health(underlyings=[underlying("SPY", "failed", "Capture is failing.")])


async def test_the_app_builds_one_projection_and_its_archive_reader_sees_the_service_installed_later() -> None:
    app = FastAPI()
    first = await get_scoped_health_service(request_to(app))
    assert await get_scoped_health_service(request_to(app)) is first

    before = await first.project(operations=frozenset({"options_history"}))
    assert [(item.severity, item.freshness) for item in before.items] == [("unknown", "unknown")]
    assert "not running" in before.items[0].explanation

    app.state.option_archive_service = StubArchive()
    after = await first.project(operations=frozenset({"options_history"}), instrument="SPY")
    assert [(item.severity, item.summary) for item in after.items] == [("critical", "SPY: Capture is failing.")]
