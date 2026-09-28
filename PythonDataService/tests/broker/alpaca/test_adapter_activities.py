"""Golden-fixture test: Alpaca activity payloads → BrokerActivity.

Fixture layout (activities.json):
  [0] — synthetic FILL trade activity (SPY buy, 2026-07-24)
  [1] — real JNLC non-trade activity (paper account funding, 2026-07-21)
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.adapter import (
    et_date_to_ms,
    from_alpaca_activity,
    rfc3339_to_ms,
)
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.contract.errors import BrokerEvidenceUnavailable, BrokerUnavailable
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


class _ActivitiesClient:
    """Minimal broker-client seam for occurred-at cursor filtering."""

    def __init__(self, pages: dict[str | None, list[dict]]) -> None:
        self.pages = pages
        self.limit: int | None = None
        self.page_tokens: list[str | None] = []
        self.activity_types: list[str | None] = []

    async def list_activities(
        self,
        *,
        limit: int,
        page_token: str | None = None,
        activity_type: str | None = None,
    ) -> list[dict]:
        self.limit = limit
        self.page_tokens.append(page_token)
        self.activity_types.append(activity_type)
        return self.pages[page_token]


class _BoundedActivitiesClient(_ActivitiesClient):
    """A cycling fake that fails the test instead of hanging forever."""

    async def list_activities(
        self,
        *,
        limit: int,
        page_token: str | None = None,
        activity_type: str | None = None,
    ) -> list[dict]:
        if len(self.page_tokens) >= 3:
            raise AssertionError("the broker followed a repeated transfer cursor")
        return await super().list_activities(
            limit=limit,
            page_token=page_token,
            activity_type=activity_type,
        )


def test_trade_activity_maps_to_trade_category(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    trade = load_alpaca_fixture("activities", "activities.json")[0]

    activity = from_alpaca_activity(trade, observed_at_ms=_OBSERVED)

    assert activity.broker == "alpaca"
    assert activity.activity_id == "20260724143000000::00000000-0000-0000-0000-000000000099"
    assert activity.activity_type == "FILL"
    assert activity.category == "trade_activity"
    assert activity.symbol == "SPY"
    assert activity.side == "buy"
    assert activity.quantity == 1.0
    assert activity.price == 737.91
    assert activity.occurred_at_ms == rfc3339_to_ms("2026-07-24T14:30:00.123456Z")
    assert activity.observed_at_ms == _OBSERVED


def test_non_trade_activity_maps_to_non_trade_category(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]

    activity = from_alpaca_activity(non_trade, observed_at_ms=_OBSERVED)

    assert activity.activity_type == "JNLC"
    assert activity.category == "non_trade_activity"
    assert activity.net_amount == 100000.0
    assert activity.side is None
    assert activity.occurred_at_ms == et_date_to_ms("2026-07-21")


async def test_activity_cursor_filters_the_contract_occurred_at_timestamp(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    client = _ActivitiesClient({None: [trade, non_trade]})
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]
    # cursor is midnight UTC on 2026-07-24: FILL (14:30 UTC) passes, JNLC (date
    # 2026-07-21 → ET open) does not.
    cursor = rfc3339_to_ms("2026-07-24T00:00:00Z")

    activities = await broker.list_activities(after_ms=cursor, limit=25)

    assert client.limit == 25
    assert [activity.activity_id for activity in activities] == [trade["id"]]


async def test_activity_cursor_paginates_a_bounded_newest_first_window(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    page_size = 25
    older_page = [
        {**non_trade, "id": f"old-{index}"}
        for index in range(page_size)
    ]
    qualifying_activity = {**trade, "id": "qualifying"}
    client = _ActivitiesClient(
        {
            None: older_page,
            "old-24": [qualifying_activity],
        }
    )
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]
    cursor = rfc3339_to_ms("2026-07-24T00:00:00Z")

    activities = await broker.list_activities(after_ms=cursor, limit=page_size)

    assert client.page_tokens == [None, "old-24"]
    assert [activity.activity_id for activity in activities] == ["qualifying"]


async def test_activity_cursor_stops_after_its_strict_page_bound(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    page_size = 25

    def older_page(prefix: str) -> list[dict]:
        return [{**non_trade, "id": f"{prefix}-{index}"} for index in range(page_size)]

    client = _ActivitiesClient(
        {
            None: older_page("page-1"),
            "page-1-24": older_page("page-2"),
            "page-2-24": older_page("page-3"),
            "page-3-24": [{**trade, "id": "must-not-fetch"}],
        }
    )
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]
    cursor = rfc3339_to_ms("2026-07-24T00:00:00Z")

    activities = await broker.list_activities(after_ms=cursor, limit=page_size)

    assert client.page_tokens == [None, "page-1-24", "page-2-24"]
    assert activities == []


async def test_transfer_cursor_reads_every_page_until_the_window_is_complete(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    page_size = 2

    def transfer_page(prefix: str, count: int = page_size) -> list[dict]:
        return [
            {
                **non_trade,
                "id": f"{prefix}-{index}",
                "activity_type": "CSD",
            }
            for index in range(count)
        ]

    client = _ActivitiesClient(
        {
            None: transfer_page("page-1"),
            "page-1-1": transfer_page("page-2"),
            "page-2-1": transfer_page("page-3"),
            "page-3-1": transfer_page("page-4", count=1),
        }
    )
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]
    cursor = rfc3339_to_ms("2026-07-21T00:00:00Z")

    activities = await broker.list_activities(
        after_ms=cursor,
        limit=page_size,
        activity_type="TRANS",
    )

    assert client.page_tokens == [None, "page-1-1", "page-2-1", "page-3-1"]
    assert client.activity_types == ["TRANS"] * 4
    assert [activity.activity_id for activity in activities] == [
        "page-1-0",
        "page-1-1",
        "page-2-0",
        "page-2-1",
        "page-3-0",
        "page-3-1",
        "page-4-0",
    ]


async def test_transfer_cursor_preserves_an_undated_row_as_incomplete_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    undated = {
        key: value
        for key, value in {**non_trade, "id": "undated", "activity_type": "CSD"}.items()
        if key not in {"date", "transaction_time"}
    }
    client = _ActivitiesClient({None: [undated]})
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]

    activities = await broker.list_activities(
        after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
        limit=25,
        activity_type="TRANS",
    )

    assert len(activities) == 1
    assert activities[0].activity_id == "undated"
    assert activities[0].occurred_at_ms is None


async def test_transfer_cursor_preserves_a_boundary_date_row_as_ambiguous_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    transfer = {**non_trade, "id": "boundary-date", "activity_type": "CSD"}
    client = _ActivitiesClient({None: [transfer]})
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]

    activities = await broker.list_activities(
        after_ms=rfc3339_to_ms("2026-07-21T20:00:00Z"),
        limit=25,
        activity_type="TRANS",
    )

    assert [activity.activity_id for activity in activities] == ["boundary-date"]


async def test_transfer_cursor_rejects_a_repeated_page_before_the_boundary(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    transfer = {**non_trade, "id": "repeated", "activity_type": "CSD"}
    client = _ActivitiesClient({None: [transfer], "repeated": [transfer]})
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]

    with pytest.raises(BrokerUnavailable, match="history was incomplete"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=1,
            activity_type="TRANS",
        )


async def test_transfer_cursor_rejects_a_multi_page_cycle(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    activity_a = {**non_trade, "id": "A", "activity_type": "CSD"}
    activity_b = {**non_trade, "id": "B", "activity_type": "CSD"}
    client = _BoundedActivitiesClient(
        {
            None: [activity_a],
            "A": [activity_b],
            "B": [activity_a],
        }
    )
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]

    with pytest.raises(BrokerUnavailable, match="history was incomplete"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=1,
            activity_type="TRANS",
        )


async def test_transfer_cursor_translates_a_malformed_row_to_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    malformed = {
        key: value
        for key, value in {**non_trade, "activity_type": "CSD"}.items()
        if key != "activity_type"
    }
    broker = AlpacaBroker(
        client=_ActivitiesClient({None: [malformed]})  # type: ignore[arg-type]
    )

    with pytest.raises(BrokerUnavailable, match="evidence was malformed"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )


async def test_transfer_cursor_translates_a_non_object_row_to_unavailable_evidence() -> None:
    broker = AlpacaBroker(
        client=_ActivitiesClient({None: [None]})  # type: ignore[list-item, arg-type]
    )

    with pytest.raises(BrokerUnavailable, match="evidence was malformed"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )


@pytest.mark.parametrize("invalid_id", [None, "", "   "])
async def test_transfer_cursor_rejects_a_missing_or_blank_activity_id(
    load_alpaca_fixture: AlpacaFixtureLoader,
    invalid_id: object,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    transfer = {
        **non_trade,
        "id": invalid_id,
        "activity_type": "CSD",
    }
    broker = AlpacaBroker(
        client=_ActivitiesClient({None: [transfer]})  # type: ignore[arg-type]
    )

    with pytest.raises(BrokerUnavailable, match="valid activity id"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )


@pytest.mark.parametrize("status", [None, "pending"])
async def test_transfer_cursor_rejects_a_transfer_that_is_not_executed(
    load_alpaca_fixture: AlpacaFixtureLoader,
    status: object,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    transfer = {
        **non_trade,
        "id": "unsettled",
        "activity_type": "CSD",
        "status": status,
    }
    broker = AlpacaBroker(
        client=_ActivitiesClient({None: [transfer]})  # type: ignore[arg-type]
    )

    with pytest.raises(BrokerUnavailable, match="was not executed"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )


@pytest.mark.parametrize(
    "net_amount",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("NaN", id="non-finite"),
        pytest.param("not-a-number", id="non-numeric"),
    ],
)
async def test_transfer_cursor_rejects_an_invalid_raw_net_amount(
    load_alpaca_fixture: AlpacaFixtureLoader,
    net_amount: object,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    transfer = {
        **non_trade,
        "id": "invalid-amount",
        "activity_type": "CSD",
        "net_amount": net_amount,
    }
    broker = AlpacaBroker(
        client=_ActivitiesClient({None: [transfer]})  # type: ignore[arg-type]
    )

    with pytest.raises(BrokerEvidenceUnavailable, match="valid net amount"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )


async def test_transfer_cursor_rejects_conflicting_duplicate_activity_ids(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    first = {
        **non_trade,
        "id": "duplicate",
        "activity_type": "CSD",
        "net_amount": "100.00",
    }
    cursor = {**non_trade, "id": "cursor", "activity_type": "CSD"}
    conflicting = {**first, "net_amount": "200.00"}
    broker = AlpacaBroker(
        client=_ActivitiesClient(  # type: ignore[arg-type]
            {
                None: [first, cursor],
                "cursor": [conflicting],
            }
        )
    )

    with pytest.raises(BrokerUnavailable, match="conflicting duplicate"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=2,
            activity_type="TRANS",
        )


async def test_transfer_cursor_deduplicates_equivalent_activity_ids(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    first = {**non_trade, "id": "duplicate", "activity_type": "CSD"}
    cursor = {**non_trade, "id": "cursor", "activity_type": "CSD"}
    broker = AlpacaBroker(
        client=_ActivitiesClient(  # type: ignore[arg-type]
            {
                None: [first, cursor],
                "cursor": [first],
            }
        )
    )

    activities = await broker.list_activities(
        after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
        limit=2,
        activity_type="TRANS",
    )

    assert [activity.activity_id for activity in activities] == ["duplicate", "cursor"]


async def test_transfer_cursor_rejects_an_in_window_row_after_the_boundary_page(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]
    newest = {
        **non_trade,
        "id": "newest",
        "activity_type": "CSD",
        "date": "2026-07-22",
    }
    before_boundary = {
        **non_trade,
        "id": "before-boundary",
        "activity_type": "CSD",
        "date": "2026-07-20",
    }
    out_of_order = {
        **non_trade,
        "id": "out-of-order",
        "activity_type": "CSD",
        "date": "2026-07-21",
    }
    broker = AlpacaBroker(
        client=_ActivitiesClient(  # type: ignore[arg-type]
            {
                None: [newest, before_boundary],
                "before-boundary": [out_of_order],
            }
        )
    )

    with pytest.raises(BrokerUnavailable, match="not newest-first"):
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T20:00:00Z"),
            limit=2,
            activity_type="TRANS",
        )
