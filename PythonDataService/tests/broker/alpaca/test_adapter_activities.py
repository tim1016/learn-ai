"""Golden-fixture test: Alpaca activity payloads → BrokerActivity.

Fixture layout (activities.json):
  [0] — synthetic FILL trade activity (SPY buy, 2026-07-24)
  [1] — real JNLC non-trade activity (paper account funding, 2026-07-21)
"""

from __future__ import annotations

from app.broker.alpaca.adapter import (
    et_date_to_ms,
    from_alpaca_activity,
    rfc3339_to_ms,
)
from app.broker.alpaca.broker import AlpacaBroker
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


class _ActivitiesClient:
    """Minimal broker-client seam for occurred-at cursor filtering."""

    def __init__(self, pages: dict[str | None, list[dict]]) -> None:
        self.pages = pages
        self.limit: int | None = None
        self.page_tokens: list[str | None] = []

    async def list_activities(
        self,
        *,
        limit: int,
        page_token: str | None = None,
    ) -> list[dict]:
        self.limit = limit
        self.page_tokens.append(page_token)
        return self.pages[page_token]


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


async def test_activity_cursor_pages_before_filtering_occurred_at(
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
