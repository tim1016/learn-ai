"""Golden-fixture test: Alpaca position payloads → BrokerPosition.

Every contract field is asserted, including the short-position sign case.

Fixture layout (positions.json):
  [0] — real SPY long position (1 share, HITL #1178)
  [1] — synthetic TSLA short position (-3 shares)
"""

from __future__ import annotations

import logging

import pytest

from app.broker.alpaca.adapter import from_alpaca_position
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.contract.errors import BrokerEvidenceUnavailable
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_from_alpaca_position_maps_long(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    long_position = load_alpaca_fixture("positions", "positions.json")[0]

    position = from_alpaca_position(long_position, observed_at_ms=_OBSERVED)

    assert position.broker == "alpaca"
    assert position.symbol == "SPY"
    assert position.asset_id == "00000000-0000-0000-0000-000000000001"
    assert position.asset_class == "us_equity"
    assert position.quantity == 1.0
    assert position.side == "long"
    assert position.average_entry_price == 737.91
    # These are fixture values; assert exact conversion from the captured payload
    # so a mapping regression cannot hide behind a type/sign-only assertion.
    assert position.market_value == float(long_position["market_value"])
    assert position.cost_basis == 737.91
    assert position.current_price == float(long_position["current_price"])
    assert position.unrealized_pl == float(long_position["unrealized_pl"])
    assert position.unrealized_plpc == float(long_position["unrealized_plpc"])
    assert position.observed_at_ms == _OBSERVED
    assert position.prior_close_price == float(long_position["lastday_price"])


def test_from_alpaca_position_maps_short_with_signed_quantity(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    short_position = load_alpaca_fixture("positions", "positions.json")[1]

    position = from_alpaca_position(short_position, observed_at_ms=_OBSERVED)

    assert position.symbol == "TSLA"
    assert position.quantity == -3.0
    assert position.side == "short"
    assert position.market_value == -735.00


def test_missing_optional_fields_become_none(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    payload = dict(load_alpaca_fixture("positions", "positions.json")[0])
    payload.pop("current_price")
    payload.pop("unrealized_plpc")
    payload.pop("lastday_price")
    payload["asset_id"] = None

    position = from_alpaca_position(payload, observed_at_ms=_OBSERVED)

    assert position.current_price is None
    assert position.prior_close_price is None
    assert position.unrealized_plpc is None
    assert position.asset_id is None


class _PositionsClient:
    """The client seam: returns the raw position rows the test built."""

    def __init__(self, payloads: object) -> None:
        self.payloads = payloads

    async def list_positions(self) -> object:
        return self.payloads


def _malformed_rows(long_position: dict) -> dict[str, object]:
    missing_qty = {key: value for key, value in long_position.items() if key != "qty"}
    return {
        "missing-field": [missing_qty],
        "boolean-quantity": [{**long_position, "qty": True}],
        "unparseable-quantity": [{**long_position, "qty": "one"}],
        "non-object-row": [long_position, None],
    }


@pytest.mark.parametrize(
    "shape", ["missing-field", "boolean-quantity", "unparseable-quantity", "non-object-row"]
)
async def test_broker_names_a_malformed_position_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    shape: str,
) -> None:
    """A malformed row once escaped as a raw KeyError/TypeError/ValueError (#2627)."""
    long_position = load_alpaca_fixture("positions", "positions.json")[0]
    broker = AlpacaBroker(client=_PositionsClient(_malformed_rows(long_position)[shape]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="position data this app could not read") as info:
        await broker.list_positions()

    assert info.value.http_status == 503
    # Owner copy stays plain; the technical reason rides on the chain.
    assert info.value.detail is not None
    assert type(info.value.__cause__).__name__ not in info.value.detail
    assert isinstance(info.value.__cause__, KeyError | TypeError | ValueError)


@pytest.mark.parametrize("field", ["symbol", "side"])
@pytest.mark.parametrize("value", [None, "", "   "], ids=["null", "blank", "whitespace"])
async def test_broker_refuses_a_position_whose_identity_is_not_text(
    load_alpaca_fixture: AlpacaFixtureLoader,
    field: str,
    value: object,
) -> None:
    """A null symbol once became a position in "None" (#2643)."""
    long_position = load_alpaca_fixture("positions", "positions.json")[0]
    broker = AlpacaBroker(client=_PositionsClient([{**long_position, field: value}]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="position data this app could not read") as info:
        await broker.list_positions()

    assert isinstance(info.value.__cause__, ValueError)
    assert f"'{field}'" in str(info.value.__cause__)


async def test_malformed_position_logs_the_adapter_cause_for_the_operator(
    load_alpaca_fixture: AlpacaFixtureLoader,
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_position = load_alpaca_fixture("positions", "positions.json")[0]
    broker = AlpacaBroker(client=_PositionsClient(_malformed_rows(long_position)["missing-field"]))  # type: ignore[arg-type]

    with (
        caplog.at_level(logging.WARNING, logger="app.broker.alpaca.broker"),
        pytest.raises(BrokerEvidenceUnavailable),
    ):
        await broker.list_positions()

    (record,) = [record for record in caplog.records if record.name == "app.broker.alpaca.broker"]
    assert record.action == "alpaca_evidence_malformed"
    assert record.evidence == "position"
    assert record.cause == "KeyError: 'qty'"
    # The traceback survives, so an adapter bug is not read only as a bad answer.
    assert record.exc_info is not None and isinstance(record.exc_info[1], KeyError)
