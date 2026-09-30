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


async def test_activity_cursor_refuses_rows_without_an_id_instead_of_merging_them(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """Two null ids once both became "None", so the walk kept one and dropped the other (#2643)."""
    trade = load_alpaca_fixture("activities", "activities.json")[0]
    first = {**trade, "id": None, "qty": "1"}
    second = {**trade, "id": None, "qty": "2"}
    broker = AlpacaBroker(client=_ActivitiesClient({None: [first, second]}))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="activity data this app could not read") as info:
        await broker.list_activities(after_ms=rfc3339_to_ms("2026-07-24T00:00:00Z"), limit=25)

    assert info.value.http_status == 503
    assert isinstance(info.value.__cause__, ValueError)
    assert "'id'" in str(info.value.__cause__)


async def test_activity_evidence_carries_provider_exhaustion_and_retains_unknown_dates(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    _, non_trade = load_alpaca_fixture("activities", "activities.json")
    client = _ActivitiesClient({None: [non_trade]})
    evidence = await AlpacaBroker(client=client).read_activity_evidence()
    assert client.limit == 100
    assert evidence.history_complete
    assert len(evidence.activities) == 1


async def test_activity_evidence_never_claims_completion_at_page_bound(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    _, non_trade = load_alpaca_fixture("activities", "activities.json")
    pages = {None: [{**non_trade, "id": f"first-{i}"} for i in range(100)],
        "first-99": [{**non_trade, "id": f"second-{i}"} for i in range(100)],
        "second-99": [{**non_trade, "id": f"third-{i}"} for i in range(100)]}
    client = _ActivitiesClient(pages)
    evidence = await AlpacaBroker(client=client).read_activity_evidence()
    assert not evidence.history_complete
    assert len(evidence.activities) == 300
    assert client.page_tokens == [None, "first-99", "second-99"]


async def test_activity_evidence_resumes_an_unfinished_walk_where_it_stopped(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """A bounded read hands back its cursor so older history is reachable (#2550)."""
    _, non_trade = load_alpaca_fixture("activities", "activities.json")
    pages = {None: [{**non_trade, "id": f"first-{i}"} for i in range(100)],
        "first-99": [{**non_trade, "id": f"second-{i}"} for i in range(100)],
        "second-99": [{**non_trade, "id": f"third-{i}"} for i in range(100)],
        "third-99": [{**non_trade, "id": "oldest"}]}
    client = _ActivitiesClient(pages)
    broker = AlpacaBroker(client=client)
    head = await broker.read_activity_evidence()
    assert not head.history_complete and head.next_page_token == "third-99"
    rest = await broker.read_activity_evidence(page_token=head.next_page_token)
    assert rest.history_complete and rest.next_page_token is None
    assert [row.activity_id for row in rest.activities] == ["oldest"]
    assert client.page_tokens == [None, "first-99", "second-99", "third-99"]


async def test_windowed_activity_evidence_is_complete_one_dated_page_after_it_crosses_the_window(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """A period read stops one proof page past its own start, not at the first older row (#2565)."""
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    in_window = [{**trade, "id": f"in-{i}"} for i in range(60)]
    older = [{**non_trade, "id": f"older-{i}"} for i in range(40)]
    fully_older = [{**non_trade, "id": f"oldest-{i}", "date": "2026-07-20"} for i in range(100)]
    client = _ActivitiesClient({
        None: in_window + older,
        "older-39": fully_older,
        "oldest-99": [{**trade, "id": "must-not-fetch"}],
    })

    evidence = await AlpacaBroker(client=client).read_activity_evidence(
        after_ms=rfc3339_to_ms("2026-07-24T00:00:00Z"),
    )

    assert evidence.history_complete and evidence.next_page_token is None
    assert [row.activity_id for row in evidence.activities] == [f"in-{i}" for i in range(60)]
    assert client.page_tokens == [None, "older-39"]


async def test_windowed_activity_evidence_refuses_an_in_window_row_after_the_crossing_page(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """Regression (#2569 review): one older row claimed completeness while a later page held an in-window row."""
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    in_window = [{**trade, "id": f"in-{i}"} for i in range(60)]
    older = [{**non_trade, "id": f"older-{i}"} for i in range(40)]
    client = _ActivitiesClient({None: in_window + older, "older-39": [{**trade, "id": "late-in-window"}]})

    with pytest.raises(BrokerEvidenceUnavailable, match="activity history was not newest-first"):
        await AlpacaBroker(client=client).read_activity_evidence(
            after_ms=rfc3339_to_ms("2026-07-24T00:00:00Z"),
        )

    assert client.page_tokens == [None, "older-39"]


async def test_windowed_activity_evidence_crossing_at_the_page_bound_is_unfinished(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """The bound leaves no page to prove the crossing, so the read hands back its cursor (#2565)."""
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    pages = {None: [{**trade, "id": f"first-{i}"} for i in range(100)],
        "first-99": [{**trade, "id": f"second-{i}"} for i in range(100)],
        "second-99": [{**trade, "id": f"third-{i}"} for i in range(60)]
        + [{**non_trade, "id": f"older-{i}"} for i in range(40)],
        "older-39": [{**trade, "id": "must-not-fetch"}]}
    client = _ActivitiesClient(pages)

    evidence = await AlpacaBroker(client=client).read_activity_evidence(
        after_ms=rfc3339_to_ms("2026-07-24T00:00:00Z"),
    )

    assert not evidence.history_complete and evidence.next_page_token == "older-39"
    assert len(evidence.activities) == 260
    assert client.page_tokens == [None, "first-99", "second-99"]


async def test_windowed_activity_evidence_undated_row_withholds_the_crossing_proof(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """An undated row cannot be ordered, so its page proves nothing and the walk goes on (#2565)."""
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    undated = {
        key: value
        for key, value in {**non_trade, "id": "undated"}.items()
        if key not in {"date", "transaction_time"}
    }
    crossing = (
        [{**trade, "id": f"in-{i}"} for i in range(60)]
        + [undated]
        + [{**non_trade, "id": f"older-{i}"} for i in range(39)]
    )
    client = _ActivitiesClient({
        None: crossing,
        "older-38": [{**non_trade, "id": f"oldest-{i}", "date": "2026-07-20"} for i in range(100)],
        "oldest-99": [{**non_trade, "id": "last", "date": "2026-07-19"}],
    })

    evidence = await AlpacaBroker(client=client).read_activity_evidence(
        after_ms=rfc3339_to_ms("2026-07-24T00:00:00Z"),
    )

    assert evidence.history_complete and evidence.next_page_token is None
    assert [row.activity_id for row in evidence.activities] == [f"in-{i}" for i in range(60)] + ["undated"]
    assert client.page_tokens == [None, "older-38", "oldest-99"]


async def test_windowed_activity_evidence_says_when_the_window_holds_more(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """Regression (#2565 review): a busy period was cut at 300 rows with no marker."""
    trade, non_trade = load_alpaca_fixture("activities", "activities.json")
    pages = {None: [{**trade, "id": f"first-{i}"} for i in range(100)],
        "first-99": [{**trade, "id": f"second-{i}"} for i in range(100)],
        "second-99": [{**trade, "id": f"third-{i}"} for i in range(100)],
        "third-99": [{**trade, "id": "oldest-in-window"}, {**non_trade, "id": "before-window"}]}
    client = _ActivitiesClient(pages)
    broker = AlpacaBroker(client=client)
    window_start = rfc3339_to_ms("2026-07-24T00:00:00Z")

    head = await broker.read_activity_evidence(after_ms=window_start)
    assert not head.history_complete and head.next_page_token == "third-99"
    assert len(head.activities) == 300

    rest = await broker.read_activity_evidence(page_token=head.next_page_token, after_ms=window_start)
    assert rest.history_complete
    assert [row.activity_id for row in rest.activities] == ["oldest-in-window"]


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

    with pytest.raises(BrokerUnavailable, match="transfer activity data this app could not read") as info:
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )

    assert info.value.detail is not None
    assert "KeyError" not in info.value.detail
    assert isinstance(info.value.__cause__, KeyError)
    assert info.value.__cause__.args == ("activity_type",)


_GENERIC_ACTIVITY_READS = {
    "newest-page": lambda broker: broker.list_activities(limit=25),
    "bounded-recovery-walk": lambda broker: broker.list_activities(
        after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"), limit=25
    ),
    "evidence-walk": lambda broker: broker.read_activity_evidence(),
    "windowed-evidence-walk": lambda broker: broker.read_activity_evidence(
        after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z")
    ),
}


@pytest.mark.parametrize("read", sorted(_GENERIC_ACTIVITY_READS))
@pytest.mark.parametrize(
    ("shape", "cause_type"),
    [
        pytest.param("missing-type", KeyError, id="missing-type"),
        pytest.param("unparseable-time", ValueError, id="unparseable-time"),
        pytest.param("non-object-row", TypeError, id="non-object-row"),
        # A null or blank identity once became the text "None" or "" (#2643).
        pytest.param("null-id", ValueError, id="null-id"),
        pytest.param("blank-id", ValueError, id="blank-id"),
        pytest.param("null-type", ValueError, id="null-type"),
        pytest.param("blank-type", ValueError, id="blank-type"),
    ],
)
async def test_generic_activity_reads_name_a_malformed_row_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    read: str,
    shape: str,
    cause_type: type[Exception],
) -> None:
    """Only the transfer walk and the account read once named a malformed answer (#2627)."""
    trade = load_alpaca_fixture("activities", "activities.json")[0]
    rows: dict[str, list[object]] = {
        "missing-type": [{key: value for key, value in trade.items() if key != "activity_type"}],
        "unparseable-time": [{**trade, "transaction_time": "not-a-time"}],
        "non-object-row": [trade, None],
        "null-id": [{**trade, "id": None}],
        "blank-id": [{**trade, "id": " "}],
        "null-type": [{**trade, "activity_type": None}],
        "blank-type": [{**trade, "activity_type": ""}],
    }
    broker = AlpacaBroker(client=_ActivitiesClient({None: rows[shape]}))  # type: ignore[arg-type, dict-item]

    with pytest.raises(BrokerEvidenceUnavailable, match="activity data this app could not read") as info:
        await _GENERIC_ACTIVITY_READS[read](broker)

    assert info.value.detail is not None
    assert cause_type.__name__ not in info.value.detail
    assert isinstance(info.value.__cause__, cause_type)


async def test_transfer_cursor_translates_a_non_object_row_to_unavailable_evidence() -> None:
    broker = AlpacaBroker(
        client=_ActivitiesClient({None: [None]})  # type: ignore[list-item, arg-type]
    )

    with pytest.raises(BrokerUnavailable, match="transfer activity data this app could not read") as info:
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )

    assert info.value.detail is not None
    assert "TypeError" not in info.value.detail
    assert isinstance(info.value.__cause__, TypeError)
    assert "must be an object" in str(info.value.__cause__)


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

    with pytest.raises(BrokerEvidenceUnavailable, match="transfer activity data this app could not read") as info:
        await broker.list_activities(
            after_ms=rfc3339_to_ms("2026-07-21T00:00:00Z"),
            limit=25,
            activity_type="TRANS",
        )

    # The adapter's one required-text check refuses it, as for every activity (#2643).
    assert isinstance(info.value.__cause__, ValueError)
    assert "'id'" in str(info.value.__cause__)


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
