"""The Research History over HTTP, with stores that hold exactly what each test needs (T17).

The endpoint's job is to give one page of the union of several stores exactly: the same record is never twice, none is
missing, the pages add up to the count, and a store that fails is named instead of reading as empty. These use stand-in
stores so the numbers, ids and instants can be chosen to collide; the real file stores are read in
``tests/research/history/test_artifact_sources.py`` and the SQL in ``test_research_history_postgres.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.data_lake import catalog_client
from app.main import app
from app.research.history.contract import HistoryQuery, SourceRead, SourceUnavailableError
from app.research.history.registry import default_history_sources
from app.routers.research_history import get_history_sources
from app.schemas.research_history import (
    HISTORY_TYPES,
    HistoryStatus,
    HistoryType,
    ResearchHistoryEntry,
)

URL = "/api/research/history"


def an_entry(
    type_: HistoryType,
    native_id: str,
    created_at_ms: int,
    *,
    status: HistoryStatus = "completed",
    symbols: tuple[str, ...] = ("SPY",),
) -> ResearchHistoryEntry:
    return ResearchHistoryEntry(
        type=type_,
        native_id=native_id,
        question=f"{type_} {native_id}",
        symbols=list(symbols),
        window=None,
        status=status,
        status_detail=None,
        created_at_ms=created_at_ms,
        completed_at_ms=None,
        provenance=None,
        headline=None,
        parent=None,
    )


class AStore:
    """A store holding fixed entries. It orders ids as a number when ``numeric``, and says how much it was asked for."""

    def __init__(
        self,
        type_: HistoryType,
        entries: list[ResearchHistoryEntry],
        *,
        numeric: bool = False,
        unreadable: int = 0,
    ) -> None:
        self.type: HistoryType = type_
        self.label = f"saved {type_} records"
        self.entries = entries
        self.numeric = numeric
        self.unreadable = unreadable
        self.asked: list[tuple[HistoryQuery, int]] = []

    def order_key(self, native_id: str) -> int | str:
        return int(native_id) if self.numeric else native_id

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        self.asked.append((query, take))
        in_time = [e for e in self.entries if query.admits_time(e.created_at_ms)]
        matching = [
            e
            for e in in_time
            if (query.symbol is None or query.symbol in e.symbols) and query.status in (None, e.status)
        ]
        matching.sort(key=lambda e: (-e.created_at_ms, self.order_key(e.native_id)))
        return SourceRead(
            total=len(matching),
            entries=tuple(matching[:take]),
            symbols=frozenset(s for e in in_time for s in e.symbols),
            unreadable=self.unreadable,
        )


class ABrokenStore(AStore):
    def __init__(self, type_: HistoryType, failure: Exception) -> None:
        super().__init__(type_, [])
        self.failure = failure

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        raise self.failure


class ALyingStore(AStore):
    """Answers out of the order every store must keep, which would corrupt a merge without any sign of it."""

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        honest = await super().read(query, take)
        return SourceRead(
            total=honest.total, entries=tuple(reversed(honest.entries)), symbols=honest.symbols, unreadable=0
        )


@pytest.fixture
def stores() -> Iterator[list[AStore]]:
    """Three stores whose ids collide as numbers and whose instants collide across stores."""
    held: list[AStore] = []
    app.dependency_overrides[get_history_sources] = lambda: held
    yield held
    app.dependency_overrides.pop(get_history_sources, None)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def colliding_stores() -> list[AStore]:
    """Numeric ids 1..12 in two stores, hash-like ids in a third, with every instant shared by all three."""
    instants = [1_700_000_000_000 + 1_000 * (i // 3) for i in range(12)]  # four instants, three records each
    return [
        AStore("feature-experiment", [an_entry("feature-experiment", str(i + 1), t) for i, t in enumerate(instants)], numeric=True),
        AStore("signal-report", [an_entry("signal-report", str(i + 1), t) for i, t in enumerate(instants)], numeric=True),
        AStore("spec-run", [an_entry("spec-run", f"{i + 1:032x}", t) for i, t in enumerate(instants)]),
    ]


def expected_order(held: list[AStore]) -> list[tuple[str, str]]:
    """The shared order, written out independently of the merge: newest first, then type, then the id as its store counts."""
    keyed = [(-e.created_at_ms, e.type, store.order_key(e.native_id), e.native_id) for store in held for e in store.entries]
    return [(type_, native_id) for _, type_, _, native_id in sorted(keyed)]


async def every_page(client: httpx.AsyncClient, page_size: int, **params: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """All pages of a query, from page 1 until the count is reached, and the first page's envelope."""
    first = (await client.get(URL, params={**params, "page": 1, "page_size": page_size})).json()
    entries = list(first["entries"])
    page = 1
    while len(entries) < first["total"]:
        page += 1
        body = (await client.get(URL, params={**params, "page": page, "page_size": page_size})).json()
        assert body["entries"], f"page {page} is empty before the count of {first['total']} is reached"
        assert body["total"] == first["total"], "the count changed between pages"
        entries.extend(body["entries"])
    return entries, first


@pytest.mark.parametrize("page_size", [1, 3, 5, 7, 36, 100])
async def test_pages_of_colliding_ids_and_equal_instants_hold_every_record_once_in_the_shared_order(
    client: httpx.AsyncClient, stores: list[AStore], page_size: int
) -> None:
    stores.extend(colliding_stores())

    entries, first = await every_page(client, page_size)

    assert [(e["type"], e["native_id"]) for e in entries] == expected_order(stores)
    assert first["total"] == 36
    assert len({(e["type"], e["native_id"]) for e in entries}) == 36, "a record appeared on two pages"


async def test_an_id_means_what_its_type_says_so_equal_numbers_in_two_stores_are_two_records(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.extend(colliding_stores()[:2])

    body = (await client.get(URL, params={"page_size": 100})).json()

    sevens = [e for e in body["entries"] if e["native_id"] == "7"]
    assert sorted(e["type"] for e in sevens) == ["feature-experiment", "signal-report"]


async def test_numeric_ids_order_as_numbers_not_as_text_inside_one_instant(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.append(AStore("signal-report", [an_entry("signal-report", str(i), 5_000) for i in (10, 9, 100, 2)], numeric=True))

    body = (await client.get(URL)).json()

    assert [e["native_id"] for e in body["entries"]] == ["2", "9", "10", "100"]


async def test_a_page_is_cut_from_the_union_not_from_each_stores_first_page(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    older = AStore("spec-run", [an_entry("spec-run", f"{i:032x}", 1_000 + i) for i in range(10)])
    newer = AStore("signal-report", [an_entry("signal-report", str(i), 2_000 + i) for i in range(10)], numeric=True)
    stores.extend([older, newer])

    body = (await client.get(URL, params={"page": 2, "page_size": 5})).json()

    # The newest ten are all the signal reports, so page 2 is their older half and no spec run is on it.
    assert [(e["type"], e["native_id"]) for e in body["entries"]] == [("signal-report", str(i)) for i in (4, 3, 2, 1, 0)]
    assert body["total"] == 20
    # Each store was asked for exactly what the union's first two pages can need from it, and no more.
    assert [take for _, take in older.asked] == [10]
    assert [take for _, take in newer.asked] == [10]


async def test_a_page_past_the_end_is_empty_and_still_states_the_true_count(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.extend(colliding_stores())

    body = (await client.get(URL, params={"page": 4, "page_size": 20})).json()

    assert body["entries"] == []
    assert body["total"] == 36


async def test_failed_cancelled_and_partial_records_are_kept_with_their_status_and_filterable(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    statuses: list[HistoryStatus] = ["completed", "failed", "cancelled", "partial", "running", "unreadable"]
    stores.append(AStore("spec-run", [an_entry("spec-run", f"{i:032x}", 100 + i, status=s) for i, s in enumerate(statuses)]))

    everything = (await client.get(URL)).json()
    partial_only = (await client.get(URL, params={"status": "partial"})).json()
    cancelled_only = (await client.get(URL, params={"status": "cancelled"})).json()

    assert sorted(e["status"] for e in everything["entries"]) == sorted(statuses)
    assert [e["status"] for e in partial_only["entries"]] == ["partial"]
    assert partial_only["total"] == 1
    assert [e["status"] for e in cancelled_only["entries"]] == ["cancelled"]


async def test_symbol_and_time_filters_narrow_the_count_and_the_pages_together(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.append(
        AStore(
            "signal-report",
            [
                an_entry("signal-report", "1", 1_000, symbols=("SPY",)),
                an_entry("signal-report", "2", 2_000, symbols=("QQQ",)),
                an_entry("signal-report", "3", 3_000, symbols=("SPY", "QQQ")),
                an_entry("signal-report", "4", 4_000, symbols=("SPY",)),
            ],
            numeric=True,
        )
    )

    by_symbol = (await client.get(URL, params={"symbol": "qqq"})).json()
    by_time = (await client.get(URL, params={"created_from_ms": 2_000, "created_before_ms": 4_000})).json()

    assert [e["native_id"] for e in by_symbol["entries"]] == ["3", "2"]
    assert by_symbol["total"] == 2
    assert [e["native_id"] for e in by_time["entries"]] == ["3", "2"]
    assert by_time["total"] == 2
    assert by_symbol["symbols"] == ["QQQ", "SPY"], "the symbol filter offers every symbol in the time range, not only the matches"


async def test_a_store_that_cannot_be_read_is_named_and_left_out_of_the_count_not_shown_as_empty(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.extend(
        [
            AStore("signal-report", [an_entry("signal-report", str(i), 100 + i) for i in range(7)], numeric=True),
            ABrokenStore("spec-run", SourceUnavailableError("its files could not be read")),
            AStore("feature-experiment", [an_entry("feature-experiment", str(i), 500 + i) for i in range(5)], numeric=True),
        ]
    )

    entries, first = await every_page(client, 4)

    assert first["partial"] is True
    assert first["total"] == 12 and len(entries) == 12
    by_type = {source["type"]: source for source in first["sources"]}
    assert by_type["spec-run"]["state"] == "unavailable"
    assert by_type["spec-run"]["total"] is None
    assert "saved spec-run records could not be read: its files could not be read" in by_type["spec-run"]["error"]
    assert by_type["signal-report"]["state"] == "read" and by_type["signal-report"]["total"] == 7
    assert by_type["feature-experiment"]["state"] == "read" and by_type["feature-experiment"]["total"] == 5


@pytest.mark.parametrize("failure", [RuntimeError("boom"), TimeoutError()], ids=["unexpected", "timeout"])
async def test_a_store_failing_in_any_way_is_named(client: httpx.AsyncClient, stores: list[AStore], failure: Exception) -> None:
    stores.extend([AStore("signal-report", [an_entry("signal-report", "1", 100)], numeric=True), ABrokenStore("spec-run", failure)])

    body = (await client.get(URL)).json()

    assert body["partial"] is True
    assert [e["native_id"] for e in body["entries"]] == ["1"]
    assert {s["type"]: s["state"] for s in body["sources"]} == {"signal-report": "read", "spec-run": "unavailable"}


async def test_a_store_holding_records_it_cannot_read_says_so_and_the_page_is_partial(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.append(AStore("spec-run", [an_entry("spec-run", f"{1:032x}", 100)], unreadable=3))

    body = (await client.get(URL)).json()

    assert body["partial"] is True
    assert body["total"] == 1
    assert body["sources"][0]["unreadable"] == 3


async def test_every_store_failing_is_a_503_naming_each_never_an_empty_page(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.extend(
        [
            ABrokenStore("spec-run", SourceUnavailableError("its files could not be read")),
            ABrokenStore("signal-report", SourceUnavailableError("the database could not be reached or queried")),
        ]
    )

    response = await client.get(URL)

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "RESEARCH_HISTORY_UNAVAILABLE"
    assert [(s["type"], s["state"]) for s in detail["sources"]] == [("spec-run", "unavailable"), ("signal-report", "unavailable")]


async def test_the_type_filter_asks_only_that_store_and_the_others_read_as_not_queried(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    held = colliding_stores()
    stores.extend(held)

    body = (await client.get(URL, params={"type": "signal-report", "page_size": 100})).json()

    assert {e["type"] for e in body["entries"]} == {"signal-report"}
    assert body["total"] == 12
    assert {s["type"]: s["state"] for s in body["sources"]} == {
        "feature-experiment": "not_queried",
        "signal-report": "read",
        "spec-run": "not_queried",
    }
    assert held[0].asked == [] and held[2].asked == []


async def test_one_failed_store_does_not_hide_when_the_type_filter_leaves_it_out(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.extend([AStore("signal-report", [an_entry("signal-report", "1", 100)], numeric=True), ABrokenStore("spec-run", RuntimeError("boom"))])

    body = (await client.get(URL, params={"type": "signal-report"})).json()

    assert body["partial"] is False
    assert body["sources"][1]["state"] == "not_queried"


async def test_a_store_answering_out_of_order_is_refused_rather_than_merged_into_a_wrong_page(
    client: httpx.AsyncClient, stores: list[AStore]
) -> None:
    stores.extend(
        [
            ALyingStore("signal-report", [an_entry("signal-report", str(i), 100 + i) for i in range(4)], numeric=True),
            AStore("spec-run", [an_entry("spec-run", f"{1:032x}", 500)]),
        ]
    )

    body = (await client.get(URL)).json()

    assert body["partial"] is True
    assert {s["type"]: s["state"] for s in body["sources"]} == {"signal-report": "unavailable", "spec-run": "read"}
    assert "inconsistent" in body["sources"][0]["error"]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": 1001},
        {"page_size": 0},
        {"page_size": 101},
        {"type": "not-a-type"},
        {"status": "not-a-status"},
        {"symbol": ""},
        {"symbol": " "},
        {"symbol": "S P Y"},
        {"symbol": "S" * 51},
        {"created_from_ms": -1},
        {"created_from_ms": 253_402_300_800_000},
        {"created_from_ms": 5_000, "created_before_ms": 5_000},
        {"created_from_ms": 6_000, "created_before_ms": 5_000},
    ],
)
async def test_a_request_that_cannot_be_answered_is_refused_before_any_store_is_read(
    client: httpx.AsyncClient, stores: list[AStore], params: dict[str, Any]
) -> None:
    held = colliding_stores()
    stores.extend(held)

    response = await client.get(URL, params=params)

    assert response.status_code == 422, response.text
    assert all(store.asked == [] for store in held)


def test_the_registry_holds_exactly_one_store_for_every_type() -> None:
    registered = [source.type for source in default_history_sources()]

    assert sorted(registered) == sorted(HISTORY_TYPES)


async def test_with_no_database_configured_the_two_database_stores_are_unavailable_and_the_file_stores_still_answer(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[get_history_sources] = lambda: default_history_sources(tmp_path)
    monkeypatch.setattr(settings, "POSTGRES_URL", "")
    await catalog_client.close_pool()
    try:
        response = await client.get(URL)
    finally:
        app.dependency_overrides.pop(get_history_sources, None)

    body = response.json()
    assert response.status_code == 200, response.text
    states = {s["type"]: s["state"] for s in body["sources"]}
    assert states["signal-report"] == states["feature-experiment"] == "unavailable"
    assert states["spec-run"] == states["walk-forward"] == states["monte-carlo"] == states["baseline"] == "read"
    assert body["partial"] is True
    assert body["total"] == 0, "an empty total here is the file stores' emptiness, and the page says the others are missing"
