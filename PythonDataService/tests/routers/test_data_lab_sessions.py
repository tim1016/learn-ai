"""Saved Data Lab sessions over HTTP, without a database.

These run on every PR: what a malformed request is told, what an unknown id is told, what each route answers on
success, the 503 of a service with no database configured, and the row-to-session mapping (legacy rows included).
The SQL itself is proved against a real database in ``test_data_lab_sessions_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.main import app
from app.routers.data_lab_sessions import get_session_store
from app.schemas.data_lab_sessions import (
    DataLabSessionReceipt,
    DataLabSessionRequest,
    DataLabSessionResponse,
    session_date_window_ms,
)
from app.services.data_lab_session_service import session_from_row

BASE = "/api/data-lab/sessions"
SESSION_ID = "6f1f3c52-1b0c-4a52-9c6e-0d6b4b8a2f10"
CREATED_MS = 1_790_000_000_000
UPDATED_MS = 1_790_000_005_000
ENTRIES = [{"name": "ema", "params": {"length": 20}}, {"name": "rsi", "params": {"length": 14}}]
SNAPSHOT = {
    "timeframe": "5m",
    "bars": [{"t": 1, "o": 1.5, "c": 1.25}],
    "visibleIndicatorIds": ["ema-20"],
    "warmupNote": None,
}


def _body(**changes: Any) -> dict[str, Any]:
    return {
        "name": "SPY week",
        "ticker": "SPY",
        "from_date": "2026-10-19",
        "to_date": "2026-10-21",
        "session": "rth",
        "forward_fill": True,
        "adjusted": False,
        "entries": ENTRIES,
        "chart_snapshot": SNAPSHOT,
        **changes,
    }


class _Sessions:
    """A store that keeps sessions in memory and keeps the contract the routes rely on: an unknown id answers None/False."""

    def __init__(self) -> None:
        self.saved: dict[uuid.UUID, DataLabSessionResponse] = {}
        self.received: list[DataLabSessionRequest] = []

    def _session(
        self, session_id: uuid.UUID, request: DataLabSessionRequest, created_at_ms: int
    ) -> DataLabSessionResponse:
        return DataLabSessionResponse(
            id=session_id,
            **request.model_dump(),
            window_start_ms=session_date_window_ms(request.from_date),
            window_end_ms=session_date_window_ms(request.to_date),
            created_at_ms=created_at_ms,
            updated_at_ms=UPDATED_MS,
        )

    async def list_sessions(self) -> list[DataLabSessionResponse]:
        return list(self.saved.values())

    async def get_session(self, session_id: uuid.UUID) -> DataLabSessionResponse | None:
        return self.saved.get(session_id)

    async def create_session(self, request: DataLabSessionRequest) -> DataLabSessionReceipt:
        self.received.append(request)
        new_id = uuid.uuid4()
        self.saved[new_id] = self._session(new_id, request, CREATED_MS)
        return DataLabSessionReceipt(id=new_id, created_at_ms=CREATED_MS, updated_at_ms=UPDATED_MS)

    async def replace_session(
        self, session_id: uuid.UUID, request: DataLabSessionRequest
    ) -> DataLabSessionReceipt | None:
        if session_id not in self.saved:
            return None
        self.received.append(request)
        self.saved[session_id] = self._session(session_id, request, self.saved[session_id].created_at_ms)
        return DataLabSessionReceipt(
            id=session_id, created_at_ms=self.saved[session_id].created_at_ms, updated_at_ms=UPDATED_MS
        )

    async def rename_session(self, session_id: uuid.UUID, name: str) -> DataLabSessionReceipt | None:
        if session_id not in self.saved:
            return None
        self.saved[session_id] = self.saved[session_id].model_copy(update={"name": name})
        return DataLabSessionReceipt(
            id=session_id, created_at_ms=self.saved[session_id].created_at_ms, updated_at_ms=UPDATED_MS
        )

    async def delete_session(self, session_id: uuid.UUID) -> bool:
        return self.saved.pop(session_id, None) is not None


@pytest.fixture
def sessions() -> Iterator[_Sessions]:
    store = _Sessions()
    app.dependency_overrides[get_session_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_session_store, None)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


# ── What a client is told when it asks wrongly ─────────────────────


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "x" * 301},
        {"ticker": ""},
        {"ticker": "T" * 21},
        {"session": ""},
        {"session": "extended-hours"},
        {"from_date": "2026-1-5"},
        {"from_date": "20261005"},
        {"from_date": "2026-02-30"},
        {"from_date": "2026-10-21T00:00:00Z"},
        {"from_date": "1969-12-31"},
        {"to_date": ""},
        {"to_date": "２０２６-10-21"},
        {"entries": {"name": "ema"}},
        {"entries": json.dumps(ENTRIES)},
        {"chart_snapshot": json.dumps(SNAPSHOT)},
        {"chart_snapshot": [1, 2]},
        {"forward_fill": "maybe"},
        {"created_at_ms": 5},
        {"id": SESSION_ID},
    ],
    ids=lambda changes: next(iter(changes)) + "=" + str(next(iter(changes.values())))[:24],
)
async def test_a_save_that_is_not_a_whole_valid_session_is_refused_with_a_422(
    client: httpx.AsyncClient, sessions: _Sessions, changes: dict[str, Any]
) -> None:
    saved = await client.post(BASE, json=_body(**changes))
    replaced = await client.put(f"{BASE}/{SESSION_ID}", json=_body(**changes))

    assert (saved.status_code, replaced.status_code) == (422, 422), (saved.text, replaced.text)
    assert sessions.received == []


@pytest.mark.parametrize("missing", ["name", "ticker", "from_date", "to_date"])
async def test_a_save_without_its_required_fields_is_refused(
    client: httpx.AsyncClient, sessions: _Sessions, missing: str
) -> None:
    body = _body()
    del body[missing]

    response = await client.post(BASE, json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", missing]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_a_document_holding_a_non_finite_number_is_refused_because_the_database_cannot_hold_it(
    client: httpx.AsyncClient, sessions: _Sessions, literal: str
) -> None:
    raw = json.dumps(_body(chart_snapshot={"bars": ["PLACEHOLDER"]})).replace('"PLACEHOLDER"', literal)

    response = await client.post(BASE, content=raw, headers={"content-type": "application/json"})

    assert response.status_code == 422, response.text
    assert sessions.received == []


async def test_an_entry_list_with_a_non_finite_number_nested_inside_is_refused(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    raw = json.dumps(_body(entries=[{"name": "ema", "params": {"length": "PLACEHOLDER"}}])).replace(
        '"PLACEHOLDER"', "NaN"
    )

    response = await client.post(BASE, content=raw, headers={"content-type": "application/json"})

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("PUT", "", _body()),
        ("PATCH", "/name", {"name": "new"}),
        ("DELETE", "", None),
    ],
)
async def test_an_id_that_is_not_a_uuid_is_refused_with_a_422(
    client: httpx.AsyncClient, sessions: _Sessions, method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    response = await client.request(method, f"{BASE}/not-a-uuid{suffix}", json=body)

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("name", ["", "x" * 301])
async def test_a_rename_to_an_unusable_name_is_refused(
    client: httpx.AsyncClient, sessions: _Sessions, name: str
) -> None:
    session_id = (await client.post(BASE, json=_body())).json()["id"]

    response = await client.patch(f"{BASE}/{session_id}/name", json={"name": name})

    assert response.status_code == 422
    assert (await client.get(f"{BASE}/{session_id}")).json()["name"] == "SPY week"


# ── An id no session has ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("PUT", "", _body()),
        ("PATCH", "/name", {"name": "new"}),
        ("DELETE", "", None),
    ],
)
async def test_an_unknown_session_is_a_404_that_names_it(
    client: httpx.AsyncClient, sessions: _Sessions, method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    response = await client.request(method, f"{BASE}/{SESSION_ID}{suffix}", json=body)

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DATA_LAB_SESSION_NOT_FOUND"
    assert SESSION_ID in detail["message"]


# ── What each route answers ────────────────────────────────────────


async def test_a_save_is_a_201_that_returns_the_id_and_the_instants_the_server_stamped(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    response = await client.post(BASE, json=_body())

    assert response.status_code == 201, response.text
    receipt = response.json()
    assert set(receipt) == {"id", "created_at_ms", "updated_at_ms"}
    assert (receipt["created_at_ms"], receipt["updated_at_ms"]) == (CREATED_MS, UPDATED_MS)
    assert uuid.UUID(receipt["id"]) in sessions.saved


async def test_the_documents_reach_the_store_as_json_values_not_as_encoded_strings(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    await client.post(BASE, json=_body())

    (received,) = sessions.received
    assert received.entries == ENTRIES
    assert received.chart_snapshot == SNAPSHOT


async def test_a_session_saved_without_a_chart_or_a_session_kind_takes_the_defaults(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    body = _body()
    del body["chart_snapshot"], body["session"], body["entries"], body["forward_fill"], body["adjusted"]

    response = await client.post(BASE, json=body)

    assert response.status_code == 201, response.text
    (received,) = sessions.received
    assert (received.chart_snapshot, received.session, received.entries, received.forward_fill, received.adjusted) == (
        None,
        "rth",
        [],
        True,
        True,
    )


async def test_a_get_returns_the_session_whole_in_snake_case_with_every_instant_in_ms(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    receipt = (await client.post(BASE, json=_body())).json()

    response = await client.get(f"{BASE}/{receipt['id']}")

    assert response.status_code == 200
    assert response.json() == {
        "id": receipt["id"],
        "name": "SPY week",
        "ticker": "SPY",
        "from_date": "2026-10-19",
        "to_date": "2026-10-21",
        "session": "rth",
        "forward_fill": True,
        "adjusted": False,
        "entries": ENTRIES,
        "chart_snapshot": SNAPSHOT,
        "window_start_ms": 1_792_368_000_000,
        "window_end_ms": 1_792_540_800_000,
        "created_at_ms": CREATED_MS,
        "updated_at_ms": UPDATED_MS,
    }


async def test_a_replace_and_a_rename_answer_the_receipt_and_change_what_a_get_returns(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    session_id = (await client.post(BASE, json=_body())).json()["id"]

    replaced = await client.put(f"{BASE}/{session_id}", json=_body(ticker="QQQ", chart_snapshot=None))
    renamed = await client.patch(f"{BASE}/{session_id}/name", json={"name": "QQQ week"})
    fetched = (await client.get(f"{BASE}/{session_id}")).json()

    assert (replaced.status_code, renamed.status_code) == (200, 200)
    assert replaced.json()["id"] == renamed.json()["id"] == session_id
    assert (fetched["ticker"], fetched["chart_snapshot"], fetched["name"]) == ("QQQ", None, "QQQ week")


async def test_a_delete_is_a_204_with_no_body_and_the_session_is_gone_after(
    client: httpx.AsyncClient, sessions: _Sessions
) -> None:
    session_id = (await client.post(BASE, json=_body())).json()["id"]

    deleted = await client.delete(f"{BASE}/{session_id}")
    again = await client.delete(f"{BASE}/{session_id}")
    fetched = await client.get(f"{BASE}/{session_id}")

    assert (deleted.status_code, deleted.content) == (204, b"")
    assert (again.status_code, fetched.status_code) == (404, 404)


async def test_the_list_answers_every_session_the_store_holds(client: httpx.AsyncClient, sessions: _Sessions) -> None:
    first = (await client.post(BASE, json=_body(name="one"))).json()["id"]
    second = (await client.post(BASE, json=_body(name="two", chart_snapshot=None))).json()["id"]

    response = await client.get(BASE)

    assert response.status_code == 200
    assert [(row["id"], row["name"]) for row in response.json()] == [(first, "one"), (second, "two")]


# ── A service with no database ─────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("GET", f"/{SESSION_ID}", None),
        ("POST", "", _body()),
        ("PUT", f"/{SESSION_ID}", _body()),
        ("PATCH", f"/{SESSION_ID}/name", {"name": "new"}),
        ("DELETE", f"/{SESSION_ID}", None),
    ],
)
async def test_with_no_database_configured_every_route_answers_a_503_without_naming_the_setting(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", "")

    response = await client.request(method, f"{BASE}{suffix}", json=body)

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "DATA_LAB_SESSIONS_STORE_UNAVAILABLE"
    assert "POSTGRES" not in response.text.upper()


# ── The window a date gives ────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1970-01-01", 0),
        ("2026-03-01", 1_772_323_200_000),
        ("2026-10-21", 1_792_540_800_000),
        ("2024-02-29", 1_709_164_800_000),
        ("9999-12-31", 253_402_214_400_000),
    ],
)
def test_a_date_is_the_utc_midnight_that_begins_it(value: str, expected: int) -> None:
    assert session_date_window_ms(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "2026-02-30",
        "2025-02-29",
        "2026-13-01",
        "2026-1-5",
        "20261021",
        "1969-12-31",
        "2026-10-21 ",
        "2026-10-21\n",
        "bad-date",
    ],
)
def test_anything_that_is_not_a_real_date_in_range_has_no_window(value: str) -> None:
    assert session_date_window_ms(value) is None


# ── A stored row as the screen sees it ─────────────────────────────


def _row(**changes: Any) -> dict[str, Any]:
    return {
        "Id": uuid.UUID(SESSION_ID),
        "Name": "SPY week",
        "Ticker": "SPY",
        "FromDate": "2026-10-19",
        "ToDate": "2026-10-21",
        "Session": "extended",
        "ForwardFill": False,
        "Adjusted": True,
        "EntriesJson": json.dumps(ENTRIES),
        "ChartSnapshotJson": json.dumps(SNAPSHOT),
        "window_start_ms": 1_792_368_000_000,
        "window_end_ms": 1_792_540_800_000,
        "created_at_ms": CREATED_MS,
        "updated_at_ms": UPDATED_MS,
        **changes,
    }


def test_a_stored_row_becomes_a_session_whose_documents_are_json_values() -> None:
    session = session_from_row(_row())

    assert session.entries == ENTRIES
    assert session.chart_snapshot == SNAPSHOT
    assert (session.session, session.forward_fill, session.adjusted) == ("extended", False, True)
    assert (session.created_at_ms, session.updated_at_ms) == (CREATED_MS, UPDATED_MS)


def test_a_session_saved_without_a_chart_has_a_null_chart_snapshot() -> None:
    assert session_from_row(_row(ChartSnapshotJson=None)).chart_snapshot is None


def test_a_stored_window_is_served_as_stored_even_when_the_dates_say_otherwise() -> None:
    session = session_from_row(_row(window_start_ms=7, window_end_ms=9))

    assert (session.window_start_ms, session.window_end_ms) == (7, 9)


def test_a_legacy_row_with_no_window_gets_the_one_its_dates_give_and_none_where_they_are_not_dates() -> None:
    session = session_from_row(_row(window_start_ms=None, window_end_ms=None, ToDate="bad-date"))

    assert (session.window_start_ms, session.window_end_ms) == (1_792_368_000_000, None)


def test_a_row_whose_entries_are_not_a_list_fails_loudly_instead_of_being_served() -> None:
    with pytest.raises(ValueError, match="entries"):
        session_from_row(_row(EntriesJson=json.dumps({"name": "ema"})))
