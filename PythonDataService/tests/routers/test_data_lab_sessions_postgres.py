"""Saved Data Lab sessions against the real ``"DataLabSessions"`` table.

These run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. Every
row a test makes has a name ending in the test's tag, and is removed afterwards. The store runs on a clock the test
moves, so the order and the stamps are exact rather than racing real time.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.data_lake import catalog_client
from app.main import app
from app.routers.data_lab_sessions import get_session_store
from app.services.data_lab_session_service import PostgresDataLabSessionStore
from tests.research.conftest import second_conn as second_conn
from tests.research.conftest import unique as unique

BASE = "/api/data-lab/sessions"
T0 = 1_790_000_000_123  # a non-zero millisecond part, so a rounding column would show
ENTRIES = [{"name": "ema", "params": {"length": 20}}, {"name": "rsi", "params": {"length": 14}}]
SNAPSHOT = {
    "timeframe": "5m",
    "label": "naïve – 日本",
    "bars": [{"t": 1_790_000_000_000, "o": 1.5, "h": 2.25, "l": 1.0, "c": 1.75, "v": 123456789.123456}],
    "flags": [True, False, None],
    "nested": {"a": {"b": [1, 2, {"c": None}]}},
    "warmupNote": None,
}
SESSION_STAMP_SQL = """
    SELECT "CreatedMsUtc", "UpdatedMsUtc", "WindowStartMsUtc", "WindowEndMsUtc",
           FLOOR(EXTRACT(EPOCH FROM "CreatedAt") * 1000)::bigint AS created_legacy_ms,
           FLOOR(EXTRACT(EPOCH FROM "UpdatedAt") * 1000)::bigint AS updated_legacy_ms,
           jsonb_typeof("EntriesJson") AS entries_kind,
           "ChartSnapshotJson" IS NULL AS no_chart,
           jsonb_typeof("ChartSnapshotJson") AS chart_kind
    FROM public."DataLabSessions" WHERE "Id" = $1
"""


@pytest.fixture
def db(second_conn: asyncpg.Connection) -> asyncpg.Connection:
    """A plain session on the attested database (the research suites' fixture skips the test without one)."""
    return second_conn


class _Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
async def tag(db: asyncpg.Connection, unique: str) -> AsyncIterator[str]:
    yield unique
    await db.execute('DELETE FROM public."DataLabSessions" WHERE "Name" LIKE $1', f"%{unique}")


@pytest.fixture
def clock() -> _Clock:
    return _Clock(T0)


@pytest.fixture
async def client(
    db: asyncpg.Connection, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The app talking to the attested database, its store on the test's clock."""
    monkeypatch.setattr(settings, "POSTGRES_URL", os.environ["POSTGRES_URL"])
    app.dependency_overrides[get_session_store] = lambda: PostgresDataLabSessionStore(clock=clock)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_session_store, None)
        await catalog_client.close_pool()


def _body(tag: str, name: str = "session", **changes: Any) -> dict[str, Any]:
    return {
        "name": f"{name} {tag}",
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


async def _mine(client: httpx.AsyncClient, tag: str) -> list[dict[str, Any]]:
    listed = await client.get(BASE)
    assert listed.status_code == 200, listed.text
    return [row for row in listed.json() if row["name"].endswith(tag)]


def _listed(session_id: str, name: str, **changes: Any) -> dict[str, Any]:
    """The list's row for a session saved by ``_body`` (two entries, a chart) unless ``changes`` say otherwise."""
    return {
        "id": session_id,
        "name": name,
        "ticker": "SPY",
        "from_date": "2026-10-19",
        "to_date": "2026-10-21",
        "entry_count": 2,
        "has_chart": True,
        "created_at_ms": T0,
        "updated_at_ms": T0,
        **changes,
    }


async def test_a_session_lives_its_whole_life_against_the_real_table(
    client: httpx.AsyncClient, db: asyncpg.Connection, clock: _Clock, tag: str
) -> None:
    saved_a = await client.post(BASE, json=_body(tag, "a"))
    clock.now += 1_000
    saved_b = await client.post(BASE, json=_body(tag, "b", chart_snapshot=None))

    assert (saved_a.status_code, saved_b.status_code) == (201, 201), (saved_a.text, saved_b.text)
    id_a, id_b = saved_a.json()["id"], saved_b.json()["id"]
    assert saved_a.json() == {"id": id_a, "created_at_ms": T0, "updated_at_ms": T0}
    assert saved_b.json()["created_at_ms"] == T0 + 1_000

    # What was stored: both instants twice (ms column and legacy column agree to the millisecond), the window as
    # UTC midnight of each date, and the documents as JSON values - not as a JSON string holding JSON.
    stored = await db.fetchrow(SESSION_STAMP_SQL, uuid.UUID(id_a))
    assert dict(stored) == {
        "CreatedMsUtc": T0,
        "UpdatedMsUtc": T0,
        "WindowStartMsUtc": 1_792_368_000_000,
        "WindowEndMsUtc": 1_792_540_800_000,
        "created_legacy_ms": T0,
        "updated_legacy_ms": T0,
        "entries_kind": "array",
        "no_chart": False,
        "chart_kind": "object",
    }
    assert (await db.fetchrow(SESSION_STAMP_SQL, uuid.UUID(id_b)))["no_chart"] is True

    fetched = await client.get(f"{BASE}/{id_a}")
    assert fetched.status_code == 200
    assert fetched.json() == {
        "id": id_a,
        "name": f"a {tag}",
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
        "created_at_ms": T0,
        "updated_at_ms": T0,
    }
    assert (await client.get(f"{BASE}/{id_b}")).json()["chart_snapshot"] is None

    # Newest edit first: b was saved a second after a. The list says how many entries a session holds and whether it
    # has a chart, and carries neither document.
    assert await _mine(client, tag) == [
        _listed(id_b, f"b {tag}", has_chart=False, created_at_ms=T0 + 1_000, updated_at_ms=T0 + 1_000),
        _listed(id_a, f"a {tag}"),
    ]

    # A rename touches the name and the update instant only; it makes a the newest edit.
    clock.now += 1_000
    renamed = await client.patch(f"{BASE}/{id_a}/name", json={"name": f"renamed {tag}"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {"id": id_a, "created_at_ms": T0, "updated_at_ms": T0 + 2_000}
    after_rename = await client.get(f"{BASE}/{id_a}")
    assert after_rename.json() == {**fetched.json(), "name": f"renamed {tag}", "updated_at_ms": T0 + 2_000}
    assert [(row["id"], row["name"]) for row in await _mine(client, tag)] == [
        (id_a, f"renamed {tag}"),
        (id_b, f"b {tag}"),
    ]
    renamed_stamps = await db.fetchrow(SESSION_STAMP_SQL, uuid.UUID(id_a))
    assert (renamed_stamps["updated_legacy_ms"], renamed_stamps["created_legacy_ms"]) == (T0 + 2_000, T0)

    # A replace changes everything the screen owns, re-derives the window, and keeps the creation instant.
    clock.now += 1_000
    replaced = await client.put(
        f"{BASE}/{id_a}",
        json=_body(
            tag,
            "qqq",
            ticker="QQQ",
            from_date="2026-03-01",
            to_date="2026-03-02",
            session="extended",
            forward_fill=False,
            entries=[],
            chart_snapshot=None,
        ),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json() == {"id": id_a, "created_at_ms": T0, "updated_at_ms": T0 + 3_000}
    assert (await client.get(f"{BASE}/{id_a}")).json() == {
        "id": id_a,
        "name": f"qqq {tag}",
        "ticker": "QQQ",
        "from_date": "2026-03-01",
        "to_date": "2026-03-02",
        "session": "extended",
        "forward_fill": False,
        "adjusted": False,
        "entries": [],
        "chart_snapshot": None,
        "window_start_ms": 1_772_323_200_000,
        "window_end_ms": 1_772_409_600_000,
        "created_at_ms": T0,
        "updated_at_ms": T0 + 3_000,
    }
    assert (await db.fetchrow(SESSION_STAMP_SQL, uuid.UUID(id_a)))["no_chart"] is True
    (replaced_row,) = [row for row in await _mine(client, tag) if row["id"] == id_a]
    assert replaced_row == _listed(
        id_a,
        f"qqq {tag}",
        ticker="QQQ",
        from_date="2026-03-01",
        to_date="2026-03-02",
        entry_count=0,
        has_chart=False,
        updated_at_ms=T0 + 3_000,
    )

    deleted = await client.delete(f"{BASE}/{id_a}")
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert (await client.get(f"{BASE}/{id_a}")).status_code == 404
    assert [row["id"] for row in await _mine(client, tag)] == [id_b]


async def test_a_session_at_every_column_limit_is_saved_and_read_back_whole(
    client: httpx.AsyncClient, tag: str
) -> None:
    name = "n" * (300 - len(tag) - 1) + " " + tag
    assert len(name) == 300

    saved = await client.post(BASE, json=_body(tag) | {"name": name, "ticker": "T" * 20, "session": "s" * 10})

    assert saved.status_code == 201, saved.text
    fetched = (await client.get(f"{BASE}/{saved.json()['id']}")).json()
    assert (fetched["name"], fetched["ticker"], fetched["session"]) == (name, "T" * 20, "s" * 10)


async def test_the_list_answers_a_summary_of_a_session_with_a_huge_chart_and_a_get_still_returns_the_chart(
    client: httpx.AsyncClient, tag: str
) -> None:
    bars = [
        {
            "t": 1_790_000_000_000 + 60_000 * i,
            "o": 100.25 + i,
            "h": 101.5 + i,
            "l": 99.75 + i,
            "c": 100.5 + i,
            "v": 1_000_003 + i,
        }
        for i in range(15_000)
    ]
    huge = {"timeframe": "1m", "bars": bars, "warmupNote": None}
    saved = await client.post(BASE, json=_body(tag, chart_snapshot=huge))
    assert saved.status_code == 201, saved.text
    session_id = saved.json()["id"]

    (row,) = await _mine(client, tag)
    fetched = await client.get(f"{BASE}/{session_id}")

    assert row == _listed(session_id, f"session {tag}")
    assert len(json.dumps(row)) < 1_000
    assert len(fetched.content) > 1_000_000
    assert fetched.json()["chart_snapshot"] == huge


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [("PUT", "", "session"), ("PATCH", "/name", "rename"), ("DELETE", "", None)],
)
async def test_an_id_no_row_has_is_a_404_on_every_write(
    client: httpx.AsyncClient, tag: str, method: str, suffix: str, payload: str | None
) -> None:
    body = {"session": _body(tag), "rename": {"name": f"new {tag}"}, None: None}[payload]

    response = await client.request(method, f"{BASE}/{uuid.uuid4()}{suffix}", json=body)

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "DATA_LAB_SESSION_NOT_FOUND"


# ── A row the C# backend wrote before the ms columns existed ───────


@pytest.fixture
async def legacy_id(db: asyncpg.Connection, tag: str) -> uuid.UUID:
    """A row with none of the four ms columns, as the first C# migration left them: the legacy timestamptz only."""
    session_id = uuid.uuid4()
    await db.execute(
        """
        INSERT INTO public."DataLabSessions"
            ("Id", "Name", "CreatedAt", "UpdatedAt", "Ticker", "FromDate", "ToDate", "Session",
             "ForwardFill", "Adjusted", "EntriesJson", "ChartSnapshotJson")
        VALUES ($1, $2, '2026-03-04 05:06:07.891234+00', '2026-03-05 06:07:08.999999+00', 'IWM', '2026-03-01', 'bad-date',
                'rth', true, true, '[]'::jsonb, NULL)
        """,
        session_id,
        f"legacy {tag}",
    )
    return session_id


LEGACY_CREATED_MS = 1_772_600_767_891  # 2026-03-04T05:06:07.891Z, the .891234 floored
LEGACY_UPDATED_MS = 1_772_690_828_999  # 2026-03-05T06:07:08.999Z, the .999999 floored, not rounded up to :09.000


async def test_a_legacy_row_with_no_ms_columns_is_served_with_the_instants_of_its_legacy_columns(
    client: httpx.AsyncClient, tag: str, legacy_id: uuid.UUID
) -> None:
    fetched = await client.get(f"{BASE}/{legacy_id}")

    assert fetched.status_code == 200, fetched.text
    legacy = fetched.json()
    assert (legacy["created_at_ms"], legacy["updated_at_ms"]) == (LEGACY_CREATED_MS, LEGACY_UPDATED_MS)
    # The window is the one its dates give; a date the column holds that is not a date has none.
    assert (legacy["window_start_ms"], legacy["window_end_ms"]) == (1_772_323_200_000, None)
    assert (legacy["entries"], legacy["chart_snapshot"]) == ([], None)
    # The list reads the same instants by the same rule, and counts no entries and no chart.
    assert await _mine(client, tag) == [
        _listed(
            str(legacy_id),
            f"legacy {tag}",
            ticker="IWM",
            from_date="2026-03-01",
            to_date="bad-date",
            entry_count=0,
            has_chart=False,
            created_at_ms=LEGACY_CREATED_MS,
            updated_at_ms=LEGACY_UPDATED_MS,
        )
    ]


async def test_a_legacy_row_sorts_among_the_new_ones_by_its_update_time(
    client: httpx.AsyncClient, clock: _Clock, tag: str, legacy_id: uuid.UUID
) -> None:
    clock.now = LEGACY_UPDATED_MS + 1_000
    newer = (await client.post(BASE, json=_body(tag, "newer"))).json()["id"]
    clock.now = LEGACY_UPDATED_MS - 1_000
    older = (await client.post(BASE, json=_body(tag, "older"))).json()["id"]

    assert [row["id"] for row in await _mine(client, tag)] == [newer, str(legacy_id), older]


@pytest.mark.parametrize("write", ["replace", "rename"])
async def test_the_first_write_to_a_legacy_row_backfills_its_creation_instant_and_keeps_it(
    client: httpx.AsyncClient, db: asyncpg.Connection, clock: _Clock, tag: str, legacy_id: uuid.UUID, write: str
) -> None:
    clock.now = LEGACY_UPDATED_MS + 60_000

    if write == "replace":
        response = await client.put(f"{BASE}/{legacy_id}", json=_body(tag, "replaced"))
    else:
        response = await client.patch(f"{BASE}/{legacy_id}/name", json={"name": f"renamed {tag}"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(legacy_id),
        "created_at_ms": LEGACY_CREATED_MS,
        "updated_at_ms": LEGACY_UPDATED_MS + 60_000,
    }
    stamps = await db.fetchrow(SESSION_STAMP_SQL, legacy_id)
    assert (stamps["CreatedMsUtc"], stamps["UpdatedMsUtc"]) == (LEGACY_CREATED_MS, LEGACY_UPDATED_MS + 60_000)
    assert stamps["updated_legacy_ms"] == LEGACY_UPDATED_MS + 60_000


@pytest.fixture(autouse=True)
def _no_leftover_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_session_store, None)
