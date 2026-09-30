"""Golden-fixture tests: Alpaca asset + clock payloads → contract models.

The clock is surfaced strictly as vendor evidence — nothing in session or
calendar logic reads it as authority (documented in the broker-contract-v2 ADR;
asserted here at the model level).

Fixture layout (assets.json):
  [0] — real NJDCY OTC ADR (active, not tradable on paper)
  [1] — synthetic DELISTED Corp (inactive)
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.adapter import (
    from_alpaca_asset,
    from_alpaca_clock,
    rfc3339_to_ms,
)
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.contract.errors import BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerClockEvidence
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


class _AssetsClockClient:
    """The client seam: returns the raw asset rows and clock answer the test built."""

    def __init__(self, *, assets: list[object] | None = None, clock: object = None) -> None:
        self.assets = assets or []
        self.clock = clock

    async def list_assets(self, *, status: str | None = None, limit: int | None) -> list[object]:
        return self.assets

    async def get_asset(self, symbol: str) -> object:
        return self.assets[0] if self.assets else None

    async def get_clock(self) -> object:
        return self.clock


def test_from_alpaca_asset_maps_every_field_active(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    active = load_alpaca_fixture("assets", "assets.json")[0]

    asset = from_alpaca_asset(active)

    assert asset.broker == "alpaca"
    assert asset.asset_id == "00000000-0000-0000-0000-000000000001"
    assert asset.symbol == "NJDCY"
    assert asset.name == "Nidec Corporation American Depositary Receipts - Sponsored"
    # Alpaca's raw "class" key maps to asset_class.
    assert asset.asset_class == "us_equity"
    assert asset.exchange == "OTC"
    assert asset.status == "active"
    # OTC ADRs are not tradable on the paper account.
    assert asset.tradable is False
    assert asset.fractionable is False
    assert asset.shortable is False
    assert asset.marginable is False


def test_from_alpaca_asset_inactive(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    inactive = load_alpaca_fixture("assets", "assets.json")[1]

    asset = from_alpaca_asset(inactive)

    assert asset.symbol == "DELISTED"
    assert asset.status == "inactive"
    assert asset.tradable is False
    assert asset.shortable is False


def test_from_alpaca_asset_accepts_the_sdk_alias_key() -> None:
    # Robust to the SDK-serialized form (`asset_class`) as well as the raw `class`.
    asset = from_alpaca_asset(
        {"id": "a", "symbol": "AAPL", "asset_class": "us_equity", "status": "active"}
    )

    assert asset.asset_class == "us_equity"


def test_from_alpaca_asset_missing_class_fails_loud() -> None:
    # No sentinel default — a missing class raises, so a schema change surfaces.
    with pytest.raises(KeyError):
        from_alpaca_asset({"id": "a", "symbol": "AAPL", "status": "active"})


def test_from_alpaca_clock_is_vendor_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("clock", "clock.json")

    clock = from_alpaca_clock(payload, observed_at_ms=_OBSERVED)

    assert isinstance(clock, BrokerClockEvidence)
    assert clock.broker == "alpaca"
    assert clock.is_open is True
    assert clock.vendor_timestamp_ms == rfc3339_to_ms("2026-07-24T10:42:48.601374373-04:00")
    assert clock.next_open_ms == rfc3339_to_ms("2026-07-27T09:30:00-04:00")
    assert clock.next_close_ms == rfc3339_to_ms("2026-07-24T16:00:00-04:00")
    assert clock.observed_at_ms == _OBSERVED


def test_from_alpaca_clock_refuses_an_open_answer_that_names_no_close() -> None:
    """#2596: liveness bounds an open answer by the close it names; one with no close fails loud."""
    with pytest.raises(ValueError, match="names no next close"):
        from_alpaca_clock(
            {"is_open": True, "timestamp": "2026-07-24T10:42:48-04:00", "next_open": None},
            observed_at_ms=_OBSERVED,
        )


def test_from_alpaca_clock_accepts_a_closed_answer_without_a_close() -> None:
    clock = from_alpaca_clock(
        {"is_open": False, "timestamp": "2026-07-24T17:00:00-04:00", "next_open": None, "next_close": None},
        observed_at_ms=_OBSERVED,
    )

    assert (clock.is_open, clock.next_close_ms) == (False, None)


_ASSET_READS = {
    "list": lambda broker: broker.list_assets(status="active", limit=100),
    "lookup": lambda broker: broker.get_asset("NJDCY"),
}


@pytest.mark.parametrize("read", sorted(_ASSET_READS))
@pytest.mark.parametrize("field", ["id", "symbol", "status"])
@pytest.mark.parametrize("value", [None, "", "   "], ids=["null", "blank", "whitespace"])
async def test_broker_refuses_an_asset_whose_identity_is_not_text(
    load_alpaca_fixture: AlpacaFixtureLoader,
    read: str,
    field: str,
    value: object,
) -> None:
    """A null symbol once became asset "None" (#2643)."""
    active = load_alpaca_fixture("assets", "assets.json")[0]
    broker = AlpacaBroker(client=_AssetsClockClient(assets=[{**active, field: value}]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="asset data this app could not read") as info:
        await _ASSET_READS[read](broker)

    assert info.value.http_status == 503
    assert isinstance(info.value.__cause__, ValueError)
    assert f"'{field}'" in str(info.value.__cause__)


@pytest.mark.parametrize("read", sorted(_ASSET_READS))
async def test_broker_names_an_asset_with_no_class_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    read: str,
) -> None:
    """The asset reads once let the adapter's raw KeyError escape as a 500 (#2643)."""
    active = load_alpaca_fixture("assets", "assets.json")[0]
    classless = {key: value for key, value in active.items() if key != "class"}
    broker = AlpacaBroker(client=_AssetsClockClient(assets=[classless]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="asset data this app could not read") as info:
        await _ASSET_READS[read](broker)

    assert info.value.detail is not None
    assert "KeyError" not in info.value.detail
    assert isinstance(info.value.__cause__, KeyError)


async def test_broker_asset_lookup_still_reads_an_unlisted_symbol_as_none() -> None:
    broker = AlpacaBroker(client=_AssetsClockClient(assets=[]))  # type: ignore[arg-type]

    assert await broker.get_asset("NOPE") is None


@pytest.mark.parametrize(
    ("clock", "cause_type"),
    [
        pytest.param(
            {"is_open": True, "timestamp": "2026-07-24T10:42:48-04:00", "next_close": None},
            ValueError,
            id="open-with-no-close",
        ),
        pytest.param({"is_open": False, "next_close": None}, KeyError, id="missing-timestamp"),
        pytest.param(None, TypeError, id="non-object-answer"),
    ],
)
async def test_broker_names_a_malformed_clock_as_unavailable_evidence(
    clock: object,
    cause_type: type[Exception],
) -> None:
    """The clock read once let the adapter's raw error escape as a 500 (#2643)."""
    broker = AlpacaBroker(client=_AssetsClockClient(clock=clock))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="market clock data this app could not read") as info:
        await broker.get_clock_evidence()

    assert info.value.http_status == 503
    assert info.value.detail is not None
    assert cause_type.__name__ not in info.value.detail
    assert isinstance(info.value.__cause__, cause_type)
