"""Golden-fixture test: Alpaca activity payloads → BrokerActivity."""

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

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.limit: int | None = None

    async def list_activities(self, *, limit: int) -> list[dict]:
        self.limit = limit
        return self.payloads


def test_trade_activity_maps_to_trade_category(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    trade = load_alpaca_fixture("activities", "activities.json")[0]

    activity = from_alpaca_activity(trade, observed_at_ms=_OBSERVED)

    assert activity.broker == "alpaca"
    assert activity.activity_id == "20220923000000000::045b3b8d-c566-4bef-b741-2bf598dd7d97"
    assert activity.activity_type == "FILL"
    assert activity.category == "trade_activity"
    assert activity.symbol == "AAPL"
    assert activity.side == "buy"
    assert activity.quantity == 10.0
    assert activity.price == 135.80
    assert activity.occurred_at_ms == rfc3339_to_ms("2022-09-23T13:30:00.123456Z")
    assert activity.observed_at_ms == _OBSERVED


def test_non_trade_activity_maps_to_non_trade_category(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    non_trade = load_alpaca_fixture("activities", "activities.json")[1]

    activity = from_alpaca_activity(non_trade, observed_at_ms=_OBSERVED)

    assert activity.activity_type == "DIV"
    assert activity.category == "non_trade_activity"
    assert activity.net_amount == 12.34
    assert activity.side is None
    assert activity.occurred_at_ms == et_date_to_ms("2022-08-19")


async def test_activity_cursor_filters_the_contract_occurred_at_timestamp(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    client = _ActivitiesClient([trade, non_trade])
    broker = AlpacaBroker(client=client)  # type: ignore[arg-type]
    cursor = rfc3339_to_ms("2022-09-23T13:30:00.123456Z")

    activities = await broker.list_activities(after_ms=cursor, limit=25)

    assert client.limit == 25
    assert [activity.activity_id for activity in activities] == [trade["id"]]
